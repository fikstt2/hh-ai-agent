import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from config import load_settings
from database import Database
from tests.test_config import VALID_ENV, write_profile
from tg_bot import AgentControl, TelegramService


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeApprovalService:
    async def approve_and_apply(self, job_id: str, user_id: int):
        raise AssertionError("approval must not be called by command tests")

    def skip(self, job_id: str, user_id: int):
        raise AssertionError("skip must not be called by command tests")


def service(tmp_path: Path, app_mode: str = "dry_run"):
    settings = load_settings(
        profile_path=write_profile(tmp_path),
        environ={**VALID_ENV, "TG_USER_ID": "42", "APP_MODE": app_mode},
    )
    settings = replace(settings, database_path=tmp_path / "agent.db")
    database = Database(settings.database_path)
    database.init()
    control = AgentControl()
    bot = FakeBot()
    telegram = TelegramService(
        settings,
        database,
        FakeApprovalService(),
        control,
        bot=bot,
        now_factory=lambda: NOW,
    )
    return telegram, database, control, bot


def add_preview_vacancy(database: Database) -> None:
    assert database.discover(
        job_id="job-1",
        title="Python <Developer>",
        company="Example & Co",
        url="https://example.com/vacancy/job-1",
        description_hash="hash",
        search_query="Python",
        discovered_at=NOW,
    )
    assert database.request_approval(
        job_id="job-1",
        cover_letter="Hello <team>",
        llm_decision=True,
        llm_reason="Relevant & local",
        confidence=0.87,
        now=NOW,
        ttl_minutes=30,
    )


def test_foreign_user_cannot_pause_or_read_configuration(tmp_path: Path) -> None:
    telegram, _, control, _ = service(tmp_path)

    reply = telegram.command("pause", user_id=99)

    assert reply == "This bot is private."
    assert not control.paused
    assert "42" not in reply
    assert "TG_USER_ID" not in reply


def test_pause_resume_and_status_use_live_state(tmp_path: Path) -> None:
    telegram, _, control, _ = service(tmp_path)

    assert telegram.command("pause", user_id=42) == "Agent paused."
    assert control.paused
    status = telegram.command("status", user_id=42)
    assert "mode: dry_run" in status
    assert "state: paused" in status
    assert telegram.command("resume", user_id=42) == "Agent resumed."
    assert not control.paused
    assert control.wake_event.is_set()


def test_dry_run_preview_has_no_action_buttons_and_escapes_html(tmp_path: Path) -> None:
    telegram, database, _, bot = service(tmp_path, "dry_run")
    add_preview_vacancy(database)

    asyncio.run(telegram.send_preview(database.get("job-1"), include_actions=False))

    message = bot.messages[0]
    assert message["reply_markup"] is None
    assert "Python &lt;Developer&gt;" in message["text"]
    assert "Example &amp; Co" in message["text"]


def test_approval_preview_has_apply_and_skip_buttons(tmp_path: Path) -> None:
    telegram, database, _, bot = service(tmp_path, "approval")
    add_preview_vacancy(database)

    asyncio.run(telegram.send_preview(database.get("job-1"), include_actions=True))

    buttons = bot.messages[0]["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == ["Откликнуться", "Пропустить"]
    assert [button.callback_data for button in buttons] == ["apply:job-1", "skip:job-1"]


def test_pending_and_stats_read_sqlite(tmp_path: Path) -> None:
    telegram, database, _, _ = service(tmp_path, "approval")
    add_preview_vacancy(database)

    assert "job-1" in telegram.command("pending", user_id=42)
    assert "pending_approval: 1" in telegram.command("stats", user_id=42)
