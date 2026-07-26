from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_analyzer import OllamaAnalyzer
from approval import ApprovalGuard, ApprovalService
from browser_backend import BrowserLaunchError, create_browser_backend
from config import ConfigError, Settings, load_settings
from database import Database, VacancyStatus
from hh_client import HHClient, PageState, VacancySummary
from logging_setup import configure_logging
from tg_bot import AgentControl, TelegramService
from vacancy_filter import title_rejection_reason


logger = logging.getLogger(__name__)


async def process_vacancy(
    summary: VacancySummary,
    settings: Settings,
    database: Database,
    hh_client: HHClient,
    analyzer: OllamaAnalyzer,
    telegram: TelegramService,
    *,
    now_factory: Callable[[], datetime] | None = None,
) -> None:
    now = (now_factory or (lambda: datetime.now(UTC)))()
    if database.get(summary.id) is not None:
        return
    details = await hh_client.read_vacancy(summary, telegram.request_captcha)
    description_hash = (
        hashlib.sha256(details.description.encode()).hexdigest()
        if details.description
        else ""
    )
    if not database.discover(
        job_id=summary.id,
        title=summary.title,
        company=details.company,
        url=summary.url,
        description_hash=description_hash,
        search_query=summary.search_query,
        discovered_at=now,
    ):
        return
    logger.info("vacancy_discovered job_id=%s", summary.id)
    if details.state is not PageState.VACANCY_LOADED:
        error = details.error or details.state.value
        database.transition(
            summary.id,
            VacancyStatus.DISCOVERED,
            VacancyStatus.APPLY_FAILED,
            error_text=error,
        )
        if details.state is PageState.CAPTCHA_DETECTED:
            await telegram.notify(f"CAPTCHA was not completed for vacancy {summary.id}.")
        logger.error("vacancy_read_failed job_id=%s state=%s", summary.id, details.state.value)
        return

    rejection = title_rejection_reason(
        summary.title, settings.profile.candidate.excluded_positions
    )
    if rejection:
        database.transition(
            summary.id,
            VacancyStatus.DISCOVERED,
            VacancyStatus.REJECTED_BY_FILTER,
            llm_reason=f"Title matched excluded term: {rejection}",
        )
        logger.info("vacancy_rejected job_id=%s source=filter", summary.id)
        return

    suitability = await analyzer.assess(summary.title, details.description)
    if not suitability.suitable:
        database.transition(
            summary.id,
            VacancyStatus.DISCOVERED,
            VacancyStatus.REJECTED_BY_LLM,
            llm_decision=False,
            llm_reason=suitability.reason,
            confidence=suitability.confidence,
        )
        logger.info("vacancy_rejected job_id=%s source=llm", summary.id)
        return

    letter = await analyzer.generate_cover_letter(summary.title, details.description)
    if not letter.strip():
        database.transition(
            summary.id,
            VacancyStatus.DISCOVERED,
            VacancyStatus.APPLY_FAILED,
            error_text="cover letter generation failed",
        )
        return

    if settings.app_mode == "dry_run":
        database.store_analysis(
            summary.id,
            cover_letter=letter,
            llm_decision=True,
            llm_reason=suitability.reason,
            confidence=suitability.confidence,
        )
        await telegram.send_preview(database.get(summary.id), include_actions=False)
        return

    if database.request_approval(
        job_id=summary.id,
        cover_letter=letter,
        llm_decision=True,
        llm_reason=suitability.reason,
        confidence=suitability.confidence,
        now=now,
        ttl_minutes=settings.approval_ttl_minutes,
    ):
        await telegram.send_preview(database.get(summary.id), include_actions=True)


async def agent_loop(
    settings: Settings,
    database: Database,
    hh_client: HHClient,
    analyzer: OllamaAnalyzer,
    telegram: TelegramService,
    control: AgentControl,
) -> None:
    while True:
        if not control.paused:
            database.expire_pending(datetime.now(UTC))
            for query in settings.profile.hh.search_queries:
                if control.paused:
                    break
                summaries = await hh_client.search_vacancies(
                    query,
                    settings.profile.hh.areas,
                    settings.profile.hh.experience_filters,
                )
                for summary in summaries:
                    if control.paused:
                        break
                    await process_vacancy(
                        summary, settings, database, hh_client, analyzer, telegram
                    )
            if not control.paused:
                await hh_client.check_messages(telegram.notify)
        try:
            await asyncio.wait_for(
                control.wake_event.wait(), settings.check_interval_minutes * 60
            )
        except TimeoutError:
            pass
        finally:
            control.wake_event.clear()


async def run(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.init()
    backend = create_browser_backend(settings)
    telegram: TelegramService | None = None
    try:
        context = await backend.start()
        guard = ApprovalGuard(settings, database)
        hh_client = HHClient(context, settings, database, guard)
        if not await hh_client.ensure_login():
            raise RuntimeError(
                "HH.ru login is required. Run with BROWSER_HEADLESS=false and sign in manually."
            )
        control = AgentControl()
        approval_service = ApprovalService(settings, database, hh_client)
        telegram = TelegramService(
            settings, database, approval_service, control
        )
        analyzer = OllamaAnalyzer(settings)
        await asyncio.gather(
            telegram.start_polling(),
            agent_loop(settings, database, hh_client, analyzer, telegram, control),
        )
    finally:
        if telegram is not None:
            await telegram.stop()
        await backend.close()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe personal HH assistant")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.env_file, args.profile)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.check_config:
        print(
            f"Configuration valid: mode={settings.app_mode}, "
            f"browser={settings.browser_backend}"
        )
        return 0
    configure_logging(settings.log_path)
    try:
        asyncio.run(run(settings))
    except (BrowserLaunchError, RuntimeError) as exc:
        logger.error("startup_failed error=%s", exc)
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.info("agent_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
