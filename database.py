from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterable


class VacancyStatus(str, Enum):
    DISCOVERED = "discovered"
    REJECTED_BY_FILTER = "rejected_by_filter"
    REJECTED_BY_LLM = "rejected_by_llm"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    APPLYING = "applying"
    SKIPPED = "skipped"
    APPLIED = "applied"
    APPLY_FAILED = "apply_failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Vacancy:
    id: str
    title: str
    company: str
    url: str
    description_hash: str
    search_query: str
    llm_decision: bool | None
    llm_reason: str
    confidence: float | None
    cover_letter: str
    status: VacancyStatus
    discovered_at: str
    approval_requested_at: str | None
    approval_expires_at: str | None
    approved_at: str | None
    applying_at: str | None
    submit_attempted_at: str | None
    applied_at: str | None
    approver_id: int | None
    error_text: str


@dataclass(frozen=True)
class ClaimResult:
    allowed: bool
    reason: str
    vacancy: Vacancy | None = None


ALLOWED_TRANSITIONS = {
    VacancyStatus.DISCOVERED: {
        VacancyStatus.REJECTED_BY_FILTER,
        VacancyStatus.REJECTED_BY_LLM,
        VacancyStatus.PENDING_APPROVAL,
        VacancyStatus.APPLY_FAILED,
    },
    VacancyStatus.PENDING_APPROVAL: {
        VacancyStatus.APPROVED,
        VacancyStatus.SKIPPED,
        VacancyStatus.EXPIRED,
    },
    VacancyStatus.APPROVED: {
        VacancyStatus.APPLYING,
        VacancyStatus.EXPIRED,
        VacancyStatus.APPLY_FAILED,
    },
    VacancyStatus.APPLYING: {
        VacancyStatus.APPLIED,
        VacancyStatus.APPLY_FAILED,
    },
}


def _as_utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        statuses = ", ".join(f"'{status.value}'" for status in VacancyStatus)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS applied_jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    url TEXT,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    msg_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS vacancies (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    description_hash TEXT NOT NULL DEFAULT '',
                    search_query TEXT NOT NULL DEFAULT '',
                    llm_decision INTEGER,
                    llm_reason TEXT NOT NULL DEFAULT '',
                    confidence REAL,
                    cover_letter TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ({statuses})),
                    discovered_at TEXT NOT NULL,
                    approval_requested_at TEXT,
                    approval_expires_at TEXT,
                    approved_at TEXT,
                    applying_at TEXT,
                    submit_attempted_at TEXT,
                    applied_at TEXT,
                    approver_id INTEGER,
                    permit_hash TEXT,
                    error_text TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vacancies_status_idx ON vacancies(status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vacancies_applied_at_idx ON vacancies(applied_at)"
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(vacancies)")
            }
            for name in ("applying_at", "submit_attempted_at"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE vacancies ADD COLUMN {name} TEXT")

    @staticmethod
    def _vacancy(row: sqlite3.Row | None) -> Vacancy | None:
        if row is None:
            return None
        return Vacancy(
            id=row["id"],
            title=row["title"],
            company=row["company"],
            url=row["url"],
            description_hash=row["description_hash"],
            search_query=row["search_query"],
            llm_decision=None if row["llm_decision"] is None else bool(row["llm_decision"]),
            llm_reason=row["llm_reason"],
            confidence=row["confidence"],
            cover_letter=row["cover_letter"],
            status=VacancyStatus(row["status"]),
            discovered_at=row["discovered_at"],
            approval_requested_at=row["approval_requested_at"],
            approval_expires_at=row["approval_expires_at"],
            approved_at=row["approved_at"],
            applying_at=row["applying_at"],
            submit_attempted_at=row["submit_attempted_at"],
            applied_at=row["applied_at"],
            approver_id=row["approver_id"],
            error_text=row["error_text"],
        )

    def discover(
        self,
        *,
        job_id: str,
        title: str,
        company: str,
        url: str,
        description_hash: str,
        search_query: str,
        discovered_at: datetime,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO vacancies (
                    id, title, company, url, description_hash, search_query,
                    status, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    title,
                    company,
                    url,
                    description_hash,
                    search_query,
                    VacancyStatus.DISCOVERED.value,
                    _iso(discovered_at),
                ),
            )
            return cursor.rowcount == 1

    def get(self, job_id: str) -> Vacancy | None:
        with self._connect() as connection:
            return self._vacancy(
                connection.execute("SELECT * FROM vacancies WHERE id = ?", (job_id,)).fetchone()
            )

    def transition(
        self,
        job_id: str,
        expected: VacancyStatus | Iterable[VacancyStatus],
        target: VacancyStatus,
        **fields: object,
    ) -> bool:
        expected_statuses = (
            (expected,) if isinstance(expected, VacancyStatus) else tuple(expected)
        )
        if not expected_statuses or any(
            target not in ALLOWED_TRANSITIONS.get(status, set()) for status in expected_statuses
        ):
            return False
        allowed_fields = {"llm_decision", "llm_reason", "confidence", "cover_letter", "error_text"}
        if unknown := set(fields) - allowed_fields:
            raise ValueError(f"Unsupported transition fields: {', '.join(sorted(unknown))}")
        assignments = ["status = ?", *(f"{name} = ?" for name in fields)]
        values = [target.value, *fields.values(), job_id, *(status.value for status in expected_statuses)]
        placeholders = ", ".join("?" for _ in expected_statuses)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE vacancies SET {', '.join(assignments)} WHERE id = ? AND status IN ({placeholders})",
                values,
            )
            return cursor.rowcount == 1

    def request_approval(
        self,
        *,
        job_id: str,
        cover_letter: str,
        llm_decision: bool,
        llm_reason: str,
        confidence: float,
        now: datetime,
        ttl_minutes: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE vacancies SET
                    status = ?, cover_letter = ?, llm_decision = ?,
                    llm_reason = ?, confidence = ?, approval_requested_at = ?,
                    approval_expires_at = ?, error_text = ''
                WHERE id = ? AND status = ?
                """,
                (
                    VacancyStatus.PENDING_APPROVAL.value,
                    cover_letter,
                    int(llm_decision),
                    llm_reason,
                    confidence,
                    _iso(now),
                    _iso(now + timedelta(minutes=ttl_minutes)),
                    job_id,
                    VacancyStatus.DISCOVERED.value,
                ),
            )
            return cursor.rowcount == 1

    def store_analysis(
        self,
        job_id: str,
        *,
        cover_letter: str,
        llm_decision: bool,
        llm_reason: str,
        confidence: float,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE vacancies SET cover_letter = ?, llm_decision = ?,
                    llm_reason = ?, confidence = ?
                WHERE id = ? AND status = ?
                """,
                (
                    cover_letter,
                    int(llm_decision),
                    llm_reason,
                    confidence,
                    job_id,
                    VacancyStatus.DISCOVERED.value,
                ),
            )
            return cursor.rowcount == 1

    def approve(
        self,
        job_id: str,
        telegram_user_id: int,
        expected_user_id: int,
        now: datetime,
    ) -> str | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM vacancies WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != VacancyStatus.PENDING_APPROVAL.value:
                return None
            if telegram_user_id != expected_user_id:
                return None
            if not row["approval_expires_at"] or row["approval_expires_at"] <= _iso(now):
                connection.execute(
                    "UPDATE vacancies SET status = ? WHERE id = ? AND status = ?",
                    (
                        VacancyStatus.EXPIRED.value,
                        job_id,
                        VacancyStatus.PENDING_APPROVAL.value,
                    ),
                )
                return None
            permit = secrets.token_urlsafe(32)
            permit_hash = hashlib.sha256(permit.encode()).hexdigest()
            cursor = connection.execute(
                """
                UPDATE vacancies SET status = ?, approved_at = ?, approver_id = ?, permit_hash = ?
                WHERE id = ? AND status = ?
                """,
                (
                    VacancyStatus.APPROVED.value,
                    _iso(now),
                    telegram_user_id,
                    permit_hash,
                    job_id,
                    VacancyStatus.PENDING_APPROVAL.value,
                ),
            )
            return permit if cursor.rowcount == 1 else None

    def skip(self, job_id: str, telegram_user_id: int, expected_user_id: int) -> bool:
        if telegram_user_id != expected_user_id:
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE vacancies SET status = ?, approver_id = ? WHERE id = ? AND status = ?",
                (
                    VacancyStatus.SKIPPED.value,
                    telegram_user_id,
                    job_id,
                    VacancyStatus.PENDING_APPROVAL.value,
                ),
            )
            return cursor.rowcount == 1

    def claim_application(
        self,
        *,
        job_id: str,
        permit: str,
        telegram_user_id: int,
        expected_user_id: int,
        app_mode: str,
        enable_real_apply: bool,
        daily_limit: int,
        now: datetime,
    ) -> ClaimResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM vacancies WHERE id = ?", (job_id,)
            ).fetchone()
            vacancy = self._vacancy(row)
            if app_mode != "approval":
                return ClaimResult(False, "app_mode", vacancy)
            if not enable_real_apply:
                return ClaimResult(False, "real_apply_disabled", vacancy)
            if row is None or row["status"] != VacancyStatus.APPROVED.value:
                return ClaimResult(False, "invalid_status", vacancy)
            if not row["approval_requested_at"] or not row["approved_at"]:
                return ClaimResult(False, "missing_approval", vacancy)
            if telegram_user_id != expected_user_id or row["approver_id"] != expected_user_id:
                return ClaimResult(False, "wrong_user", vacancy)
            if not row["approval_expires_at"] or row["approval_expires_at"] <= _iso(now):
                connection.execute(
                    "UPDATE vacancies SET status = ?, permit_hash = NULL WHERE id = ? AND status = ?",
                    (VacancyStatus.EXPIRED.value, job_id, VacancyStatus.APPROVED.value),
                )
                return ClaimResult(False, "expired", vacancy)
            if not row["cover_letter"].strip():
                return ClaimResult(False, "empty_letter", vacancy)
            supplied_hash = hashlib.sha256(permit.encode()).hexdigest()
            if not secrets.compare_digest(row["permit_hash"] or "", supplied_hash):
                return ClaimResult(False, "invalid_permit", vacancy)
            if self._reserved_today(connection, now) >= daily_limit:
                return ClaimResult(False, "daily_limit", vacancy)
            cursor = connection.execute(
                "UPDATE vacancies SET status = ?, applying_at = ? WHERE id = ? AND status = ? AND permit_hash = ?",
                (
                    VacancyStatus.APPLYING.value,
                    _iso(now),
                    job_id,
                    VacancyStatus.APPROVED.value,
                    row["permit_hash"],
                ),
            )
            claimed = cursor.rowcount == 1
            return ClaimResult(
                claimed,
                "allowed" if claimed else "concurrent_claim",
                vacancy,
            )

    def complete_application(
        self,
        job_id: str,
        permit: str,
        *,
        success: bool,
        now: datetime,
        error_text: str = "",
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            supplied_hash = hashlib.sha256(permit.encode()).hexdigest()
            cursor = connection.execute(
                """
                UPDATE vacancies SET status = ?, applied_at = ?, error_text = ?, permit_hash = NULL
                WHERE id = ? AND status = ? AND permit_hash = ?
                """,
                (
                    (VacancyStatus.APPLIED if success else VacancyStatus.APPLY_FAILED).value,
                    _iso(now) if success else None,
                    "" if success else error_text,
                    job_id,
                    VacancyStatus.APPLYING.value,
                    supplied_hash,
                ),
            )
            return cursor.rowcount == 1

    def mark_submit_attempt(
        self,
        job_id: str,
        permit: str,
        *,
        now: datetime,
        daily_limit: int,
    ) -> bool:
        supplied_hash = hashlib.sha256(permit.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM vacancies WHERE id = ?", (job_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != VacancyStatus.APPLYING.value
                or not secrets.compare_digest(row["permit_hash"] or "", supplied_hash)
            ):
                return False
            if not row["approval_expires_at"] or row["approval_expires_at"] <= _iso(now):
                connection.execute(
                    "UPDATE vacancies SET status = ?, permit_hash = NULL WHERE id = ? AND status = ? AND permit_hash = ?",
                    (
                        VacancyStatus.EXPIRED.value,
                        job_id,
                        VacancyStatus.APPLYING.value,
                        row["permit_hash"],
                    ),
                )
                return False
            start, end = self._day_bounds(now)
            this_row_reserved = any(
                timestamp is not None and start <= timestamp < end
                for timestamp in (row["applying_at"], row["submit_attempted_at"])
            )
            if self._reserved_today(connection, now) - int(this_row_reserved) >= daily_limit:
                return False
            cursor = connection.execute(
                """
                UPDATE vacancies SET submit_attempted_at = ?
                WHERE id = ? AND status = ? AND permit_hash = ?
                    AND submit_attempted_at IS NULL
                """,
                (
                    _iso(now),
                    job_id,
                    VacancyStatus.APPLYING.value,
                    row["permit_hash"],
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _day_bounds(now: datetime) -> tuple[str, str]:
        aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now
        start = datetime.combine(aware.date(), time.min, tzinfo=aware.tzinfo)
        return _iso(start), _iso(start + timedelta(days=1))

    def _applied_today(self, connection: sqlite3.Connection, now: datetime) -> int:
        start, end = self._day_bounds(now)
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM vacancies WHERE status = ? AND applied_at >= ? AND applied_at < ?",
                (VacancyStatus.APPLIED.value, start, end),
            ).fetchone()[0]
        )

    def _reserved_today(self, connection: sqlite3.Connection, now: datetime) -> int:
        start, end = self._day_bounds(now)
        return int(
            connection.execute(
                """
                SELECT COUNT(*) FROM vacancies
                WHERE (status = ? AND applied_at >= ? AND applied_at < ?)
                   OR (status = ? AND applying_at >= ? AND applying_at < ?)
                   OR (status IN (?, ?) AND submit_attempted_at >= ? AND submit_attempted_at < ?)
                """,
                (
                    VacancyStatus.APPLIED.value,
                    start,
                    end,
                    VacancyStatus.APPLYING.value,
                    start,
                    end,
                    VacancyStatus.APPLYING.value,
                    VacancyStatus.APPLY_FAILED.value,
                    start,
                    end,
                ),
            ).fetchone()[0]
        )

    def applied_today(self, now: datetime) -> int:
        with self._connect() as connection:
            return self._applied_today(connection, now)

    def expire_pending(self, now: datetime) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE vacancies SET status = ?, permit_hash = NULL
                WHERE status IN (?, ?) AND approval_expires_at <= ?
                """,
                (
                    VacancyStatus.EXPIRED.value,
                    VacancyStatus.PENDING_APPROVAL.value,
                    VacancyStatus.APPROVED.value,
                    _iso(now),
                ),
            )
            return cursor.rowcount

    def pending(self) -> list[Vacancy]:
        with self._connect() as connection:
            return [
                self._vacancy(row)
                for row in connection.execute(
                    "SELECT * FROM vacancies WHERE status = ? ORDER BY discovered_at",
                    (VacancyStatus.PENDING_APPROVAL.value,),
                ).fetchall()
            ]

    def stats(self) -> dict[str, int]:
        counts = {status.value: 0 for status in VacancyStatus}
        with self._connect() as connection:
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM vacancies GROUP BY status"
            ):
                counts[row["status"]] = row["count"]
        return counts

    def is_message_processed(self, message_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM chat_messages WHERE msg_id = ?", (message_id,)
                ).fetchone()
                is not None
            )

    def add_processed_message(self, message_id: str, chat_id: str, text_value: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO chat_messages (msg_id, chat_id, text) VALUES (?, ?, ?)",
                (message_id, chat_id, text_value),
            )
            return cursor.rowcount == 1
