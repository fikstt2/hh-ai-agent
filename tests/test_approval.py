import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from approval import ApplicationPermission, ApprovalGuard, ApprovalService
from config import load_settings
from database import Database, VacancyStatus
from hh_client import HHClient
from tests.test_config import VALID_ENV, write_profile


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


class NeverPageContext:
    async def new_page(self):
        raise AssertionError("browser must not be touched for a blocked application")


class FakeLocator:
    def __init__(self, visible: bool = True, clicks: list[str] | None = None, name: str = ""):
        self.visible = visible
        self.clicks = clicks
        self.name = name
        self.first = self
        self.filled = ""

    async def is_visible(self) -> bool:
        return self.visible

    async def click(self) -> None:
        if self.clicks is not None:
            self.clicks.append(self.name)

    async def wait_for(self, **kwargs) -> None:
        if not self.visible:
            raise RuntimeError("not visible")

    async def fill(self, value: str) -> None:
        self.filled = value

    async def count(self) -> int:
        return int(self.visible)

    def or_(self, other: "FakeLocator") -> "FakeLocator":
        return self if self.visible else other


class FakeApplicationPage:
    def __init__(
        self,
        fail_navigation: bool = False,
        success_visible: bool = True,
        employer_questions: bool = False,
        response_still_available: bool = False,
    ):
        self.fail_navigation = fail_navigation
        self.success_visible = success_visible
        self.employer_questions = employer_questions
        self.response_still_available = response_still_available
        self.clicks: list[str] = []
        self.selectors: list[str] = []
        self.closed = False

    async def goto(self, url: str, **kwargs) -> None:
        if self.fail_navigation:
            raise RuntimeError("navigation failed")

    def locator(self, selector: str) -> FakeLocator:
        self.selectors.append(selector)
        if selector == '[data-qa="vacancy-description"]':
            return FakeLocator(True)
        if selector == 'textarea[name^="task_"]':
            return FakeLocator(self.employer_questions)
        if "vacancy-response-success" in selector:
            return FakeLocator(self.success_visible)
        if "vacancy-response-link" in selector:
            submitted = "final_submit" in self.clicks
            visible = not submitted or self.response_still_available
            return FakeLocator(visible, self.clicks, "open_response")
        if "resume-select" in selector:
            return FakeLocator(False)
        if "letter-toggle" in selector or "сопроводительное" in selector:
            return FakeLocator(False)
        if selector in {"textarea", 'textarea:not([name^="task_"])'}:
            return FakeLocator(True)
        if "vacancy-response-submit" in selector:
            return FakeLocator(True, self.clicks, "final_submit")
        return FakeLocator(False)

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        return FakeLocator(
            text == "Отклик отправлен" and self.success_visible
        )

    async def close(self) -> None:
        self.closed = True


class FakeApplicationContext:
    def __init__(self, page: FakeApplicationPage):
        self.page = page

    async def new_page(self) -> FakeApplicationPage:
        return self.page


class FailingApplicationContext:
    async def new_page(self):
        raise RuntimeError("browser page creation failed")


async def no_sleep(_: float) -> None:
    return None


def settings(tmp_path: Path, **environment: str):
    loaded = load_settings(
        profile_path=write_profile(tmp_path),
        environ={**VALID_ENV, **environment},
    )
    return replace(loaded, database_path=tmp_path / "agent.db")


def pending(database: Database, job_id: str = "job-1", letter: str = "Letter") -> None:
    assert database.discover(
        job_id=job_id,
        title="Python developer",
        company="Example",
        url=f"https://example.com/vacancy/{job_id}",
        description_hash="hash",
        search_query="Python",
        discovered_at=NOW,
    )
    assert database.request_approval(
        job_id=job_id,
        cover_letter=letter,
        llm_decision=True,
        llm_reason="Relevant",
        confidence=0.9,
        now=NOW,
        ttl_minutes=30,
    )


@pytest.mark.parametrize("backend", ["cloakbrowser", "playwright"])
def test_dry_run_blocks_low_level_send_independently_of_backend(
    tmp_path: Path, backend: str
) -> None:
    app_settings = settings(tmp_path, BROWSER_BACKEND=backend, APP_MODE="dry_run")
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    token = database.approve("job-1", 42, 42, NOW + timedelta(minutes=1))
    assert token
    client = HHClient(
        NeverPageContext(),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=2)),
        sleep=no_sleep,
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", token, 42))
    )

    assert not sent
    assert database.get("job-1").status is VacancyStatus.APPROVED


def test_direct_low_level_call_without_individual_permission_is_blocked(
    tmp_path: Path,
) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    client = HHClient(
        NeverPageContext(),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=1)),
        sleep=no_sleep,
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", "forged", 42))
    )

    assert not sent
    assert database.get("job-1").status is VacancyStatus.PENDING_APPROVAL


def test_approval_service_is_the_valid_path_to_physical_submit(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true", TG_USER_ID="42"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    page = FakeApplicationPage()
    client = HHClient(
        FakeApplicationContext(page),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=2)),
        sleep=no_sleep,
        now_factory=lambda: NOW + timedelta(minutes=3),
    )
    service = ApprovalService(
        app_settings,
        database,
        client,
        now_factory=lambda: NOW + timedelta(minutes=1),
    )

    result = asyncio.run(service.approve_and_apply("job-1", 42))

    assert result.ok
    assert page.clicks == ["open_response", "final_submit"]
    assert all("text=" not in selector for selector in page.selectors)
    assert database.get("job-1").status is VacancyStatus.APPLIED


def test_browser_error_after_claim_becomes_apply_failed(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true", TG_USER_ID="42"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    token = database.approve("job-1", 42, 42, NOW + timedelta(minutes=1))
    assert token
    page = FakeApplicationPage(fail_navigation=True)
    client = HHClient(
        FakeApplicationContext(page),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=2)),
        sleep=no_sleep,
        now_factory=lambda: NOW + timedelta(minutes=3),
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", token, 42))
    )

    vacancy = database.get("job-1")
    assert not sent
    assert vacancy.status is VacancyStatus.APPLY_FAILED
    assert "navigation failed" in vacancy.error_text


def test_page_creation_error_after_claim_becomes_apply_failed(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true", TG_USER_ID="42"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    token = database.approve("job-1", 42, 42, NOW + timedelta(minutes=1))
    assert token
    client = HHClient(
        FailingApplicationContext(),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=2)),
        sleep=no_sleep,
        now_factory=lambda: NOW + timedelta(minutes=3),
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", token, 42))
    )

    vacancy = database.get("job-1")
    assert not sent
    assert vacancy.status is VacancyStatus.APPLY_FAILED
    assert "page creation failed" in vacancy.error_text


def test_missing_explicit_success_signal_fails_closed(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true", TG_USER_ID="42"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    token = database.approve("job-1", 42, 42, NOW + timedelta(minutes=1))
    assert token
    page = FakeApplicationPage(success_visible=False)
    client = HHClient(
        FakeApplicationContext(page),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=2)),
        sleep=no_sleep,
        now_factory=lambda: NOW + timedelta(minutes=3),
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", token, 42))
    )

    assert not sent
    assert page.clicks == ["open_response", "final_submit"]
    assert database.get("job-1").status is VacancyStatus.APPLY_FAILED


def test_employer_questionnaire_blocks_submit(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true", TG_USER_ID="42"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    token = database.approve("job-1", 42, 42, NOW + timedelta(minutes=1))
    assert token
    page = FakeApplicationPage(employer_questions=True)
    client = HHClient(
        FakeApplicationContext(page),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=2)),
        sleep=no_sleep,
        now_factory=lambda: NOW + timedelta(minutes=3),
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", token, 42))
    )

    assert not sent
    assert page.clicks == ["open_response"]
    assert database.get("job-1").status is VacancyStatus.APPLY_FAILED


def test_success_marker_without_hh_confirmation_fails_closed(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true", TG_USER_ID="42"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    token = database.approve("job-1", 42, 42, NOW + timedelta(minutes=1))
    assert token
    page = FakeApplicationPage(response_still_available=True)
    client = HHClient(
        FakeApplicationContext(page),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=2)),
        sleep=no_sleep,
        now_factory=lambda: NOW + timedelta(minutes=3),
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", token, 42))
    )

    assert not sent
    assert page.clicks == ["open_response", "final_submit"]
    assert database.get("job-1").status is VacancyStatus.APPLY_FAILED


def test_permission_is_rechecked_for_expiry_immediately_before_submit(
    tmp_path: Path,
) -> None:
    app_settings = settings(
        tmp_path, APP_MODE="approval", ENABLE_REAL_APPLY="true", TG_USER_ID="42"
    )
    database = Database(app_settings.database_path)
    database.init()
    pending(database)
    token = database.approve("job-1", 42, 42, NOW + timedelta(minutes=29))
    assert token
    page = FakeApplicationPage()
    client = HHClient(
        FakeApplicationContext(page),
        app_settings,
        database,
        ApprovalGuard(app_settings, database, now_factory=lambda: NOW + timedelta(minutes=29)),
        sleep=no_sleep,
        now_factory=lambda: NOW + timedelta(minutes=31),
    )

    sent = asyncio.run(
        client.submit_application(ApplicationPermission("job-1", token, 42))
    )

    assert not sent
    assert page.clicks == ["open_response"]
    assert database.get("job-1").status is VacancyStatus.EXPIRED


def test_action_delay_never_drops_below_configured_minimum(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    database = Database(app_settings.database_path)
    database.init()
    delays: list[float] = []

    async def capture_delay(seconds: float) -> None:
        delays.append(seconds)

    client = HHClient(
        NeverPageContext(),
        app_settings,
        database,
        ApprovalGuard(app_settings, database),
        sleep=capture_delay,
    )

    asyncio.run(client._delay())

    assert len(delays) == 1
    assert delays[0] >= app_settings.min_seconds_between_actions
