from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from approval import ApprovalService
from config import Settings
from database import Database, Vacancy


logger = logging.getLogger(__name__)
PRIVATE_REPLY = "This bot is private."


@dataclass
class AgentControl:
    paused: bool = False
    wake_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class TelegramService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        approval_service: ApprovalService,
        control: AgentControl,
        *,
        bot: Any | None = None,
        dispatcher: Dispatcher | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.database = database
        self.approval_service = approval_service
        self.control = control
        self.bot = bot or Bot(token=settings.tg_bot_token)
        self.dispatcher = dispatcher or Dispatcher()
        self.now_factory = now_factory or (lambda: datetime.now().astimezone())
        self._captcha_future: asyncio.Future[str | None] | None = None
        self._register_handlers()

    def authorized(self, user_id: int) -> bool:
        return user_id == self.settings.tg_user_id

    def command(self, name: str, user_id: int) -> str:
        if not self.authorized(user_id):
            return PRIVATE_REPLY
        if name == "start":
            return "Personal HH assistant is ready. Use /status to inspect it."
        if name == "pause":
            self.control.paused = True
            logger.info("agent_paused")
            return "Agent paused."
        if name == "resume":
            self.control.paused = False
            self.control.wake_event.set()
            logger.info("agent_resumed")
            return "Agent resumed."
        if name == "status":
            stats = self.database.stats()
            processed = sum(stats.values())
            return (
                f"mode: {self.settings.app_mode}\n"
                f"state: {'paused' if self.control.paused else 'running'}\n"
                f"processed: {processed}\n"
                f"applied today: {self.database.applied_today(self.now_factory())}"
            )
        if name == "pending":
            pending = self.database.pending()
            return (
                "No pending vacancies."
                if not pending
                else "\n".join(f"{item.id}: {item.title}" for item in pending)
            )
        if name == "stats":
            return "\n".join(
                f"{status}: {count}"
                for status, count in self.database.stats().items()
                if count
            ) or "No vacancies recorded."
        if name == "cancel":
            if self._captcha_future and not self._captcha_future.done():
                self._captcha_future.set_result(None)
            return "Current CAPTCHA request cancelled."
        return "Unknown command."

    def _register_handlers(self) -> None:
        for name in ("start", "status", "pause", "resume", "pending", "stats", "cancel"):
            self.dispatcher.message.register(self._command_handler, Command(name))
        self.dispatcher.callback_query.register(
            self._callback_handler, F.data.startswith("apply:") | F.data.startswith("skip:")
        )
        self.dispatcher.message.register(self._text_handler)

    async def _command_handler(self, message: Message) -> None:
        name = (message.text or "").split()[0].lstrip("/").split("@")[0]
        await message.answer(self.command(name, message.from_user.id))

    async def _callback_handler(self, callback: CallbackQuery) -> None:
        if not self.authorized(callback.from_user.id):
            await callback.answer(PRIVATE_REPLY, show_alert=True)
            return
        action, job_id = (callback.data or "").split(":", 1)
        if action == "apply":
            result = await self.approval_service.approve_and_apply(
                job_id, callback.from_user.id
            )
        else:
            result = self.approval_service.skip(job_id, callback.from_user.id)
        await callback.answer(result.message, show_alert=not result.ok)
        if result.ok and callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    async def _text_handler(self, message: Message) -> None:
        if not self.authorized(message.from_user.id):
            await message.answer(PRIVATE_REPLY)
            return
        if self._captcha_future and not self._captcha_future.done() and message.text:
            self._captcha_future.set_result(message.text.strip())
            await message.answer("CAPTCHA input received.")

    async def send_preview(self, vacancy: Vacancy, include_actions: bool) -> None:
        keyboard = None
        if include_actions:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Откликнуться", callback_data=f"apply:{vacancy.id}"
                        ),
                        InlineKeyboardButton(
                            text="Пропустить", callback_data=f"skip:{vacancy.id}"
                        ),
                    ]
                ]
            )
        confidence = "n/a" if vacancy.confidence is None else f"{vacancy.confidence:.0%}"
        text = (
            f"<b>{html.escape(vacancy.title)}</b>\n"
            f"Company: {html.escape(vacancy.company or 'unknown')}\n"
            f"<a href=\"{html.escape(vacancy.url, quote=True)}\">Open vacancy</a>\n\n"
            f"Why it fits: {html.escape(vacancy.llm_reason)}\n"
            f"Confidence: {confidence}\n\n"
            f"<b>Cover letter</b>\n{html.escape(vacancy.cover_letter)}"
        )
        await self.bot.send_message(
            chat_id=self.settings.tg_user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        logger.info(
            "%s job_id=%s",
            "approval_requested" if include_actions else "preview_sent",
            vacancy.id,
        )

    async def notify(self, text: str) -> None:
        await self.bot.send_message(chat_id=self.settings.tg_user_id, text=text)

    async def request_captcha(
        self, screenshot: Path, title: str, timeout_seconds: int
    ) -> str | None:
        loop = asyncio.get_running_loop()
        self._captcha_future = loop.create_future()
        await self.bot.send_photo(
            chat_id=self.settings.tg_user_id,
            photo=FSInputFile(screenshot),
            caption=f"CAPTCHA detected for: {title}\nReply with the text or use /cancel.",
        )
        try:
            return await asyncio.wait_for(self._captcha_future, timeout_seconds)
        except TimeoutError:
            return None
        finally:
            self._captcha_future = None

    async def start_polling(self) -> None:
        await self.dispatcher.start_polling(self.bot)

    async def stop(self) -> None:
        session = getattr(self.bot, "session", None)
        if session is not None:
            await session.close()
