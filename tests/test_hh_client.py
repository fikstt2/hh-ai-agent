import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from approval import ApprovalGuard
from config import load_settings
from database import Database
from hh_client import HHClient, PageState, VacancySummary, classify_page
from tests.test_config import VALID_ENV, write_profile
from vacancy_filter import title_rejection_reason


class FakeLocator:
    def __init__(self, visible: bool):
        self.visible = visible
        self.first = self

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        return ""


class FakePage:
    def __init__(self, visible_selector: str = "", broken: bool = False):
        self.visible_selector = visible_selector
        self.broken = broken

    def locator(self, selector: str) -> FakeLocator:
        if self.broken:
            raise RuntimeError("page closed")
        return FakeLocator(selector == self.visible_selector)

    async def goto(self, url: str, **kwargs) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeContext:
    def __init__(self, page: FakePage):
        self.page = page

    async def new_page(self) -> FakePage:
        return self.page


class FailingContext:
    async def new_page(self):
        raise RuntimeError("page creation failed")


class RetryLoginPage(FakePage):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def goto(self, url: str, **kwargs) -> None:
        self.attempts += 1
        if self.attempts < 3:
            raise RuntimeError("temporary navigation failure")


class CaptchaLocator:
    def __init__(self, page: "CaptchaPage"):
        self.page = page
        self.first = self

    async def is_visible(self) -> bool:
        return True

    async def fill(self, value: str) -> None:
        self.page.solution = value

    async def press(self, key: str) -> None:
        self.page.actions.append(f"press:{key}")

    async def click(self) -> None:
        self.page.actions.append("click:submit")
        self.page.solved = True


class CaptchaPage:
    def __init__(self):
        self.actions: list[str] = []
        self.solution = ""
        self.solved = False

    def locator(self, selector: str):
        if selector == 'input[type="text"]' or "button" in selector:
            return CaptchaLocator(self)
        return FakeLocator(
            (self.solved and selector == '[data-qa="vacancy-description"]')
            or (not self.solved and selector == 'form[action*="captcha"]')
        )

    async def screenshot(self, *, path: Path) -> None:
        path.touch()


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ('[data-qa="vacancy-description"]', PageState.VACANCY_LOADED),
        ('form[action*="captcha"]', PageState.CAPTCHA_DETECTED),
        ('[data-qa="access-denied"]', PageState.ACCESS_DENIED),
        ('[data-qa="vacancy-removed"]', PageState.VACANCY_REMOVED),
        ("", PageState.PAGE_STRUCTURE_CHANGED),
    ],
)
def test_page_state_uses_explicit_signals(selector: str, expected: PageState) -> None:
    assert asyncio.run(classify_page(FakePage(selector))) is expected


def test_page_state_reports_network_error() -> None:
    assert asyncio.run(classify_page(FakePage(broken=True))) is PageState.NETWORK_ERROR


def test_visible_but_empty_description_reports_structure_change(tmp_path: Path) -> None:
    settings = replace(
        load_settings(profile_path=write_profile(tmp_path), environ=VALID_ENV),
        database_path=tmp_path / "agent.db",
        min_seconds_between_actions=0,
    )
    database = Database(settings.database_path)
    database.init()
    client = HHClient(
        FakeContext(FakePage('[data-qa="vacancy-description"]')),
        settings,
        database,
        ApprovalGuard(settings, database),
    )
    summary = VacancySummary(
        "job-1", "Developer", "https://example.com/vacancy/job-1", "Python"
    )

    result = asyncio.run(client.read_vacancy(summary))

    assert result.state is PageState.PAGE_STRUCTURE_CHANGED


def test_page_creation_failure_reports_network_error(tmp_path: Path) -> None:
    settings = replace(
        load_settings(profile_path=write_profile(tmp_path), environ=VALID_ENV),
        database_path=tmp_path / "agent.db",
    )
    database = Database(settings.database_path)
    database.init()
    client = HHClient(
        FailingContext(), settings, database, ApprovalGuard(settings, database)
    )
    summary = VacancySummary(
        "job-1", "Developer", "https://example.com/vacancy/job-1", "Python"
    )

    result = asyncio.run(client.read_vacancy(summary))

    assert result.state is PageState.NETWORK_ERROR
    assert "page creation failed" in result.error


def test_login_check_retries_temporary_navigation_errors(tmp_path: Path) -> None:
    settings = replace(
        load_settings(profile_path=write_profile(tmp_path), environ=VALID_ENV),
        database_path=tmp_path / "agent.db",
        min_seconds_between_actions=0,
    )
    database = Database(settings.database_path)
    database.init()
    page = RetryLoginPage()

    async def no_sleep(_seconds: float) -> None:
        return None

    client = HHClient(
        FakeContext(page),
        settings,
        database,
        ApprovalGuard(settings, database),
        sleep=no_sleep,
    )

    assert asyncio.run(client.ensure_login())
    assert page.attempts == 3


def test_captcha_solution_uses_submit_button(tmp_path: Path) -> None:
    settings = replace(
        load_settings(profile_path=write_profile(tmp_path), environ=VALID_ENV),
        database_path=tmp_path / "agent.db",
        min_seconds_between_actions=0,
    )
    database = Database(settings.database_path)
    database.init()
    page = CaptchaPage()
    client = HHClient(
        FakeContext(page), settings, database, ApprovalGuard(settings, database)
    )
    summary = VacancySummary(
        "job-1", "Developer", "https://example.com/vacancy/job-1", "Python"
    )

    async def solve(*_args):
        return "abcd"

    result = asyncio.run(client._solve_captcha(page, summary, solve))

    assert result is PageState.VACANCY_LOADED
    assert page.solution == "abcd"
    assert page.actions == ["click:submit"]


@pytest.mark.parametrize(
    ("title", "excluded", "expected"),
    [
        ("Senior Python developer", (), "senior"),
        ("Python sales engineer", ("sales",), "sales"),
        ("Python developer", (), None),
    ],
)
def test_title_filter_returns_the_matched_reason(
    title: str, excluded: tuple[str, ...], expected: str | None
) -> None:
    assert title_rejection_reason(title, excluded) == expected
