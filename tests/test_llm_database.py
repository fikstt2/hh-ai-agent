import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from database import Database


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


def make_database(path: Path) -> Database:
    database = Database(path)
    database.init()
    return database


def reserve(database: Database, operation: str, limit: int = 10) -> int | None:
    return database.reserve_llm_request(
        provider="fake",
        model="test-model",
        operation=operation,
        now=NOW,
        daily_limit=limit,
    )


def test_request_reservation_persists_and_in_progress_counts_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent.db"
    first = make_database(path)

    request_id = reserve(first, "vacancy_analysis", limit=1)

    assert isinstance(request_id, int)
    reopened = Database(path)
    assert reopened.llm_requests_today(NOW) == 1
    assert reserve(reopened, "cover_letter", limit=1) is None


def test_daily_request_limit_is_reserved_atomically(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.db")

    with ThreadPoolExecutor(max_workers=2) as pool:
        request_ids = list(
            pool.map(lambda operation: reserve(database, operation, limit=1), ("a", "b"))
        )

    assert sum(isinstance(request_id, int) for request_id in request_ids) == 1
    assert database.llm_requests_today(NOW) == 1


def test_daily_request_limit_uses_callers_local_calendar_day(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.db")
    moscow = timezone(timedelta(hours=3))
    after_midnight = datetime(2026, 7, 27, 0, 30, tzinfo=moscow)

    assert database.reserve_llm_request(
        provider="fake",
        model="test-model",
        operation="a",
        now=after_midnight,
        daily_limit=1,
    )

    assert database.llm_requests_today(after_midnight) == 1
    assert database.llm_requests_today(after_midnight - timedelta(hours=1)) == 0


def test_completion_records_only_usage_metadata(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    database = make_database(path)
    request_id = reserve(database, "vacancy_analysis")
    assert request_id is not None

    assert database.complete_llm_request(
        request_id,
        success=True,
        finished_at=NOW + timedelta(seconds=1),
        input_tokens=12,
        output_tokens=5,
        latency_ms=123,
        provider_request_id="provider-request-1",
    )
    assert not database.complete_llm_request(
        request_id,
        success=False,
        finished_at=NOW + timedelta(seconds=2),
        error_type="timeout",
    )

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(llm_requests)")
        }
        row = connection.execute("SELECT * FROM llm_requests").fetchone()

    assert not columns & {"prompt", "response", "api_key", "profile", "vacancy", "path"}
    assert dict(row) == {
        "id": request_id,
        "provider": "fake",
        "model": "test-model",
        "operation": "vacancy_analysis",
        "started_at": NOW.isoformat(),
        "finished_at": (NOW + timedelta(seconds=1)).isoformat(),
        "success": 1,
        "input_tokens": 12,
        "output_tokens": 5,
        "latency_ms": 123,
        "provider_request_id": "provider-request-1",
        "error_type": "",
    }


def test_failure_and_unknown_usage_remain_queryable(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.db")
    request_id = reserve(database, "cover_letter")
    assert request_id is not None

    assert database.complete_llm_request(
        request_id,
        success=False,
        finished_at=NOW + timedelta(seconds=1),
        error_type="timeout",
    )

    assert database.llm_usage_stats() == {
        "total_requests": 1,
        "successful_requests": 0,
        "failed_requests": 1,
        "input_tokens": 0,
        "output_tokens": 0,
    }
