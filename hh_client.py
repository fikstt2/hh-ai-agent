from __future__ import annotations

import asyncio
import logging
import random
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from approval import ApplicationPermission, ApprovalGuard
from config import Settings
from database import Database


logger = logging.getLogger(__name__)


class PageState(str, Enum):
    VACANCY_LOADED = "vacancy_loaded"
    CAPTCHA_DETECTED = "captcha_detected"
    ACCESS_DENIED = "access_denied"
    VACANCY_REMOVED = "vacancy_removed"
    PAGE_STRUCTURE_CHANGED = "page_structure_changed"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True)
class VacancySummary:
    id: str
    title: str
    url: str
    search_query: str


@dataclass(frozen=True)
class VacancyDetails:
    summary: VacancySummary
    state: PageState
    company: str = ""
    description: str = ""
    error: str = ""


CaptchaSolver = Callable[[Path, str, int], Awaitable[str | None]]


async def classify_page(page: Any) -> PageState:
    try:
        checks = (
            (PageState.CAPTCHA_DETECTED, ('form[action*="captcha"]', '[data-qa="captcha"]')),
            (PageState.ACCESS_DENIED, ('[data-qa="access-denied"]',)),
            (PageState.VACANCY_REMOVED, ('[data-qa="vacancy-removed"]',)),
            (PageState.VACANCY_LOADED, ('[data-qa="vacancy-description"]',)),
        )
        for state, selectors in checks:
            for selector in selectors:
                if await page.locator(selector).is_visible():
                    return state
        return PageState.PAGE_STRUCTURE_CHANGED
    except Exception:
        return PageState.NETWORK_ERROR


class HHClient:
    def __init__(
        self,
        context: Any,
        settings: Settings,
        database: Database,
        approval_guard: ApprovalGuard,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.context = context
        self.settings = settings
        self.database = database
        self.approval_guard = approval_guard
        self.sleep = sleep
        self.now_factory = now_factory or (lambda: datetime.now(UTC))

    async def _delay(self) -> None:
        await self.sleep(
            self.settings.min_seconds_between_actions + random.uniform(0.5, 2.5)
        )

    async def ensure_login(self) -> bool:
        page = await self.context.new_page()
        try:
            for attempt in range(1, 4):
                try:
                    await page.goto(
                        "https://hh.ru/",
                        wait_until="domcontentloaded",
                        timeout=90_000,
                    )
                    login = page.locator(
                        'a:has-text("Войти"), button:has-text("Войти")'
                    ).first
                    if not await login.is_visible():
                        return True
                    if self.settings.browser_headless:
                        logger.error("hh_login_required headless=true")
                        return False
                    await asyncio.to_thread(
                        input,
                        "Sign in to HH.ru in the opened browser, then press Enter here: ",
                    )
                    await page.reload(wait_until="domcontentloaded")
                    return not await login.is_visible()
                except Exception as exc:
                    logger.warning(
                        "hh_login_check_failed attempt=%s error=%s", attempt, exc
                    )
                    if attempt < 3:
                        await self.sleep(120)
            return False
        finally:
            await page.close()

    async def _response_confirmed(self, page: Any, url: str) -> bool:
        try:
            await page.goto(url.split("?")[0], wait_until="domcontentloaded", timeout=30_000)
            await page.locator('[data-qa="vacancy-description"]').wait_for(
                state="visible", timeout=20_000
            )
            response_control = page.locator(
                'a[data-qa="vacancy-response-link-top"], '
                'button[data-qa="vacancy-response-link-top"]'
            ).first
            return await response_control.count() == 0
        except Exception as exc:
            logger.warning("application_confirmation_failed error=%s", exc)
            return False

    async def search_vacancies(
        self,
        query: str,
        areas: tuple[str, ...],
        experience_filters: tuple[str, ...],
    ) -> list[VacancySummary]:
        results: list[VacancySummary] = []
        page = await self.context.new_page()
        try:
            for page_number in range(self.settings.max_pages_per_query):
                params: dict[str, Any] = {
                    "text": query,
                    "order_by": "publication_time",
                    "page": page_number,
                }
                if areas:
                    params["area"] = list(areas)
                if experience_filters:
                    params["experience"] = list(experience_filters)
                await self._delay()
                await page.goto(
                    f"https://hh.ru/search/vacancy?{urlencode(params, doseq=True)}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                cards = await page.locator('a[data-qa="serp-item__title"]').all()
                if not cards:
                    break
                for card in cards:
                    href = await card.get_attribute("href")
                    title = (await card.inner_text()).strip()
                    if not href:
                        continue
                    job_id = urlparse(href).path.rstrip("/").split("/")[-1]
                    if job_id and self.database.get(job_id) is None:
                        results.append(VacancySummary(job_id, title, href, query))
                    if len(results) >= self.settings.max_vacancies_per_query:
                        return results
                await self._delay()
            return results
        except Exception as exc:
            logger.error("vacancy_search_failed query=%r error=%s", query, exc)
            return results
        finally:
            await page.close()

    async def read_vacancy(
        self,
        summary: VacancySummary,
        captcha_solver: CaptchaSolver | None = None,
    ) -> VacancyDetails:
        page = None
        try:
            page = await self.context.new_page()
            await self._delay()
            await page.goto(summary.url, wait_until="domcontentloaded", timeout=30_000)
            state = await classify_page(page)
            if state is PageState.CAPTCHA_DETECTED and captcha_solver is not None:
                state = await self._solve_captcha(page, summary, captcha_solver)
            if state is not PageState.VACANCY_LOADED:
                if state is PageState.CAPTCHA_DETECTED:
                    logger.warning("captcha_detected job_id=%s", summary.id)
                return VacancyDetails(summary, state)
            description = (
                await page.locator('[data-qa="vacancy-description"]').inner_text()
            ).strip()
            if not description:
                return VacancyDetails(
                    summary,
                    PageState.PAGE_STRUCTURE_CHANGED,
                    error="vacancy description is empty",
                )
            company_locator = page.locator('[data-qa="vacancy-company-name"]')
            company = (
                (await company_locator.inner_text()).strip()
                if await company_locator.is_visible()
                else ""
            )
            return VacancyDetails(summary, state, company, description)
        except Exception as exc:
            return VacancyDetails(summary, PageState.NETWORK_ERROR, error=str(exc))
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "vacancy_page_close_failed job_id=%s error=%s",
                        summary.id,
                        exc,
                    )

    async def _solve_captcha(
        self, page: Any, summary: VacancySummary, solver: CaptchaSolver
    ) -> PageState:
        for _ in range(self.settings.captcha_max_attempts):
            screenshot = Path(tempfile.gettempdir()) / f"hh-captcha-{uuid4().hex}.png"
            try:
                await page.screenshot(path=screenshot)
                solution = await solver(
                    screenshot, summary.title, self.settings.captcha_timeout_seconds
                )
                if not solution:
                    return PageState.CAPTCHA_DETECTED
                field = page.locator('input[type="text"]').first
                if not await field.is_visible():
                    return PageState.PAGE_STRUCTURE_CHANGED
                await field.fill(solution)
                submit = page.locator(
                    'button[type="submit"]:visible, button:has-text("Отправить"):visible'
                ).first
                if await submit.is_visible():
                    await submit.click()
                else:
                    await field.press("Enter")
                await self.sleep(1)
                state = await classify_page(page)
                if state is not PageState.CAPTCHA_DETECTED:
                    return state
            finally:
                screenshot.unlink(missing_ok=True)
        return PageState.CAPTCHA_DETECTED

    async def submit_application(self, permission: ApplicationPermission) -> bool:
        claim = self.approval_guard.claim(permission)
        if not claim.allowed or claim.vacancy is None:
            logger.warning(
                "application_blocked job_id=%s reason=%s",
                permission.job_id,
                claim.reason,
            )
            return False

        vacancy = claim.vacancy
        page = None
        try:
            page = await self.context.new_page()
            await self._delay()
            await page.goto(vacancy.url, wait_until="domcontentloaded", timeout=30_000)
            response_button = page.locator(
                'a[data-qa="vacancy-response-link-top"], button[data-qa="vacancy-response-link-top"]'
            ).first
            if not await response_button.is_visible():
                raise RuntimeError("application response control was not found")
            await response_button.click()
            await self._delay()

            resume_name = self.settings.profile.hh.resume_name
            resume_selector = page.locator(
                '[data-qa*="resume-select"], [data-qa*="resume-selector"]'
            ).first
            if resume_name and await resume_selector.is_visible():
                await resume_selector.click()
                option = page.get_by_text(resume_name, exact=True).first
                if not await option.is_visible():
                    raise RuntimeError("configured resume was not found")
                await option.click()

            if await page.locator('textarea[name^="task_"]').count() > 0:
                raise RuntimeError("employer questionnaire requires a manual application")

            letter_toggle = (
                page.locator('[data-qa*="letter-toggle"]')
                .or_(page.get_by_text("Написать сопроводительное", exact=True))
                .or_(page.get_by_text("Добавить сопроводительное", exact=True))
                .first
            )
            if await letter_toggle.is_visible():
                await letter_toggle.click()
            textarea = page.locator('textarea:not([name^="task_"])').first
            await textarea.wait_for(state="visible", timeout=3_000)
            await textarea.fill(vacancy.cover_letter)
            await self._delay()

            submit_button = page.locator(
                'button[data-qa*="vacancy-response-submit"]:visible'
            ).first
            if not await submit_button.is_visible():
                raise RuntimeError("final application button was not found")
            if not self.database.mark_submit_attempt(
                permission.job_id,
                permission.permit,
                now=self.now_factory(),
                daily_limit=self.settings.max_applications_per_day,
            ):
                error = "application permission failed final pre-submit validation"
                self.database.complete_application(
                    permission.job_id,
                    permission.permit,
                    success=False,
                    now=self.now_factory(),
                    error_text=error,
                )
                logger.warning(
                    "application_blocked job_id=%s reason=pre_submit_recheck",
                    permission.job_id,
                )
                return False
            await submit_button.click()
            success_marker = (
                page.locator(
                    '[data-qa="vacancy-response-success"], '
                    '[data-qa="vacancy-response-link-view-topic"]'
                )
                .or_(page.get_by_text("Отклик отправлен", exact=False))
                .first
            )
            await success_marker.wait_for(state="visible", timeout=5_000)
            if not await self._response_confirmed(page, vacancy.url):
                raise RuntimeError("HH.ru did not confirm the application")
            if not self.database.complete_application(
                permission.job_id,
                permission.permit,
                success=True,
                now=self.now_factory(),
            ):
                raise RuntimeError("application status could not be completed")
            logger.info("application_sent job_id=%s", permission.job_id)
            return True
        except Exception as exc:
            self.database.complete_application(
                permission.job_id,
                permission.permit,
                success=False,
                now=self.now_factory(),
                error_text=str(exc),
            )
            logger.error("application_failed job_id=%s error=%s", permission.job_id, exc)
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "application_page_close_failed job_id=%s error=%s",
                        permission.job_id,
                        exc,
                    )

    async def check_messages(
        self, notifier: Callable[[str], Awaitable[None]]
    ) -> None:
        page = await self.context.new_page()
        try:
            await self._delay()
            await page.goto(
                "https://hh.ru/applicant/negotiations",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            cards = await page.locator('div[data-qa="negotiations-item"]').all()
            for card in cards:
                badge = card.locator('span[data-qa="negotiations-item-badge"]')
                if not await badge.is_visible():
                    continue
                link = card.locator('a[data-qa="negotiations-item-vacancy-link"]')
                href = await link.get_attribute("href")
                title = (await link.inner_text()).strip()
                message_id = f"{href}:{title}"
                if href and not self.database.is_message_processed(message_id):
                    self.database.add_processed_message(message_id, href, title)
                    await notifier(f"New unread HH message for: {title}\nhttps://hh.ru{href}")
        except Exception as exc:
            logger.error("message_check_failed error=%s", exc)
        finally:
            await page.close()
