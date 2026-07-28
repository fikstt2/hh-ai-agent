import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from database import Database, VacancyStatus


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    database.init()
    return database


def discover(database: Database, job_id: str = "job-1", letter: str = "Letter") -> None:
    assert database.discover(
        job_id=job_id,
        title="Python developer",
        company="Example",
        url=f"https://example.com/vacancy/{job_id}",
        description_hash="abc123",
        search_query="Python",
        discovered_at=NOW,
    )
    if letter:
        assert database.request_approval(
            job_id=job_id,
            cover_letter=letter,
            llm_decision=True,
            llm_reason="Profile matches",
            confidence=0.8,
            now=NOW,
            ttl_minutes=30,
        )


def approve(database: Database, job_id: str = "job-1", letter: str = "Letter") -> str:
    discover(database, job_id, letter)
    token = database.approve(
        job_id=job_id,
        telegram_user_id=42,
        expected_user_id=42,
        now=NOW + timedelta(minutes=1),
    )
    assert token
    return token


def claim(database: Database, job_id: str, token: str, **overrides: object):
    arguments = {
        "job_id": job_id,
        "permit": token,
        "telegram_user_id": 42,
        "expected_user_id": 42,
        "app_mode": "approval",
        "enable_real_apply": True,
        "daily_limit": 5,
        "now": NOW + timedelta(minutes=2),
    }
    arguments.update(overrides)
    return database.claim_application(**arguments)


def test_status_transitions_are_conditional_and_terminal(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    discover(database, letter="")

    assert database.get("job-1").status is VacancyStatus.DISCOVERED
    assert database.transition(
        "job-1", VacancyStatus.DISCOVERED, VacancyStatus.REJECTED_BY_FILTER
    )
    assert not database.transition(
        "job-1", VacancyStatus.DISCOVERED, VacancyStatus.PENDING_APPROVAL
    )
    assert database.get("job-1").status is VacancyStatus.REJECTED_BY_FILTER


def test_duplicate_discovery_is_rejected(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    discover(database, letter="")

    assert not database.discover(
        job_id="job-1",
        title="Duplicate",
        company="Other",
        url="https://example.com/duplicate",
        description_hash="different",
        search_query="Other",
        discovered_at=NOW,
    )
    assert database.get("job-1").title == "Python developer"


def test_expired_pending_approval_cannot_be_approved(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    discover(database)

    token = database.approve(
        job_id="job-1",
        telegram_user_id=42,
        expected_user_id=42,
        now=NOW + timedelta(minutes=31),
    )

    assert token is None
    assert database.get("job-1").status is VacancyStatus.EXPIRED


def test_foreign_user_cannot_approve_or_skip(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    discover(database)

    assert (
        database.approve(
            job_id="job-1",
            telegram_user_id=99,
            expected_user_id=42,
            now=NOW + timedelta(minutes=1),
        )
        is None
    )
    assert not database.skip("job-1", telegram_user_id=99, expected_user_id=42)
    assert database.get("job-1").status is VacancyStatus.PENDING_APPROVAL


def test_claim_rechecks_mode_feature_flag_and_user(tmp_path: Path) -> None:
    for name, override in (
        ("dry", {"app_mode": "dry_run"}),
        ("disabled", {"enable_real_apply": False}),
        ("foreign", {"telegram_user_id": 99}),
    ):
        database = Database(tmp_path / f"{name}.db")
        database.init()
        token = approve(database)

        result = claim(database, "job-1", token, **override)

        assert not result.allowed
        assert database.get("job-1").status is VacancyStatus.APPROVED


def test_invalid_or_reused_permit_cannot_claim_twice(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    token = approve(database)

    assert not claim(database, "job-1", "forged").allowed
    assert claim(database, "job-1", token).allowed
    assert not claim(database, "job-1", token).allowed
    assert database.get("job-1").status is VacancyStatus.APPLYING


def test_empty_letter_and_expired_permission_are_blocked(tmp_path: Path) -> None:
    empty_database = Database(tmp_path / "empty.db")
    empty_database.init()
    discover(empty_database, letter="")
    assert empty_database.request_approval(
        job_id="job-1",
        cover_letter="   ",
        llm_decision=True,
        llm_reason="Profile matches",
        confidence=0.8,
        now=NOW,
        ttl_minutes=30,
    )
    empty_token = empty_database.approve("job-1", 42, 42, NOW + timedelta(minutes=1))
    assert empty_token
    assert not claim(empty_database, "job-1", empty_token).allowed

    expired_database = Database(tmp_path / "expired.db")
    expired_database.init()
    token = approve(expired_database)
    result = claim(
        expired_database,
        "job-1",
        token,
        now=NOW + timedelta(minutes=31),
    )
    assert not result.allowed
    assert expired_database.get("job-1").status is VacancyStatus.EXPIRED


def test_daily_limit_uses_persisted_applied_rows(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    database = Database(path)
    database.init()
    first_token = approve(database, "job-1")
    assert claim(database, "job-1", first_token, daily_limit=1).allowed
    assert database.complete_application(
        "job-1", first_token, success=True, now=NOW + timedelta(minutes=3)
    )

    reopened = Database(path)
    second_token = approve(reopened, "job-2")
    second_claim = claim(reopened, "job-2", second_token, daily_limit=1)

    assert reopened.applied_today(NOW + timedelta(minutes=4)) == 1
    assert not second_claim.allowed
    assert reopened.get("job-2").status is VacancyStatus.APPROVED


def test_daily_limit_atomically_counts_in_progress_claims(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    first_token = approve(database, "job-1")
    second_token = approve(database, "job-2")

    assert claim(database, "job-1", first_token, daily_limit=1).allowed
    second_claim = claim(database, "job-2", second_token, daily_limit=1)

    assert not second_claim.allowed
    assert second_claim.reason == "daily_limit"
    assert database.get("job-2").status is VacancyStatus.APPROVED


def test_only_the_claimant_permit_can_complete_application(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    token = approve(database)
    assert claim(database, "job-1", token).allowed

    assert not database.complete_application(
        "job-1", "forged", success=True, now=NOW + timedelta(minutes=3)
    )
    assert database.get("job-1").status is VacancyStatus.APPLYING
    assert database.complete_application(
        "job-1", token, success=True, now=NOW + timedelta(minutes=3)
    )


def test_failed_final_submit_attempt_keeps_daily_reservation(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    first_token = approve(database, "job-1")
    second_token = approve(database, "job-2")
    assert claim(database, "job-1", first_token, daily_limit=1).allowed
    assert database.mark_submit_attempt(
        "job-1", first_token, now=NOW + timedelta(minutes=3), daily_limit=1
    )
    assert database.complete_application(
        "job-1",
        first_token,
        success=False,
        now=NOW + timedelta(minutes=3),
        error_text="success signal missing",
    )

    assert not claim(database, "job-2", second_token, daily_limit=1).allowed


def test_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    token = approve(database)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(database, "job-1", token), range(2)))

    assert [result.allowed for result in results].count(True) == 1
    assert database.get("job-1").status is VacancyStatus.APPLYING


def test_legacy_applied_jobs_is_preserved_and_not_imported(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE applied_jobs (id TEXT PRIMARY KEY, title TEXT, url TEXT, applied_at TEXT)"
        )
        connection.execute(
            "INSERT INTO applied_jobs VALUES (?, ?, ?, ?)",
            ("legacy", "Rejected in old version", "https://example.com/legacy", "2026-07-20"),
        )

    database = Database(path)
    database.init()

    assert database.get("legacy") is None
    assert database.applied_today(NOW) == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT title FROM applied_jobs").fetchone()[0] == "Rejected in old version"
