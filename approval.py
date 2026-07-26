from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from config import Settings
from database import ClaimResult, Database


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApplicationPermission:
    job_id: str
    permit: str
    telegram_user_id: int


@dataclass(frozen=True)
class ApprovalResult:
    ok: bool
    message: str


class PhysicalApplicationSender(Protocol):
    async def submit_application(self, permission: ApplicationPermission) -> bool: ...


class ApprovalGuard:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.database = database
        self.now_factory = now_factory or (lambda: datetime.now().astimezone())

    def claim(self, permission: ApplicationPermission) -> ClaimResult:
        return self.database.claim_application(
            job_id=permission.job_id,
            permit=permission.permit,
            telegram_user_id=permission.telegram_user_id,
            expected_user_id=self.settings.tg_user_id,
            app_mode=self.settings.app_mode,
            enable_real_apply=self.settings.enable_real_apply,
            daily_limit=self.settings.max_applications_per_day,
            now=self.now_factory(),
        )


class ApprovalService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        sender: PhysicalApplicationSender,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.database = database
        self.sender = sender
        self.now_factory = now_factory or (lambda: datetime.now().astimezone())

    async def approve_and_apply(
        self, job_id: str, telegram_user_id: int
    ) -> ApprovalResult:
        if telegram_user_id != self.settings.tg_user_id:
            logger.warning("application_blocked job_id=%s reason=wrong_user", job_id)
            return ApprovalResult(False, "Action is not allowed")
        if self.settings.app_mode != "approval":
            logger.warning("application_blocked job_id=%s reason=app_mode", job_id)
            return ApprovalResult(False, "Real applications are disabled in dry-run")
        if not self.settings.enable_real_apply:
            logger.warning("application_blocked job_id=%s reason=real_apply_disabled", job_id)
            return ApprovalResult(False, "Real applications are disabled")
        permit = self.database.approve(
            job_id,
            telegram_user_id,
            self.settings.tg_user_id,
            self.now_factory(),
        )
        if permit is None:
            logger.warning("application_blocked job_id=%s reason=approval_invalid", job_id)
            return ApprovalResult(False, "Approval is missing, expired, or already used")
        logger.info("approval_received job_id=%s", job_id)
        permission = ApplicationPermission(job_id, permit, telegram_user_id)
        sent = await self.sender.submit_application(permission)
        return ApprovalResult(sent, "Application sent" if sent else "Application failed")

    def skip(self, job_id: str, telegram_user_id: int) -> ApprovalResult:
        if telegram_user_id != self.settings.tg_user_id:
            return ApprovalResult(False, "Action is not allowed")
        skipped = self.database.skip(
            job_id, telegram_user_id, self.settings.tg_user_id
        )
        return ApprovalResult(skipped, "Vacancy skipped" if skipped else "Vacancy is not pending")
