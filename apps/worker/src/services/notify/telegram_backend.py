"""Telegram NotifyBackend — delivers every event through ``services.telegram``.

Typed events are rendered as rich Markdown cards by ``services.telegram_rich``
(plan approvals carry inline Approve / Reject buttons). If a rich send fails —
e.g. Telegram rejects the Markdown — the plain-text default from the base
class is tried instead, so a notification is never lost to formatting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from config import settings
from services import telegram, telegram_rich

from .base import NotifyBackend

logger = logging.getLogger(__name__)


class TelegramBackend(NotifyBackend):
    channel = "telegram"

    def is_configured(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    async def send_text(self, message: str) -> bool:
        if await telegram.tg_send(message):
            return True
        # Most failures here are Telegram rejecting the Markdown ("can't parse
        # entities") — an unescaped ``_`` / ``*`` in a key, title or summary.
        # Resend once as plain text so the notification is not lost.
        return await telegram.tg_send(message, parse_mode="")

    async def _rich(self, formatter: Callable[..., str], fallback: Any, **kwargs: Any) -> bool:
        """Send ``formatter(**kwargs)``; on failure run the plain-text ``fallback``."""
        try:
            if await telegram.tg_send(formatter(**kwargs)):
                return True
        except Exception:
            logger.exception("rich Telegram %s failed — falling back", formatter.__name__)
        return await fallback(**kwargs)

    async def send_task_start(self, **kwargs: Any) -> bool:
        return await self._rich(telegram_rich.format_task_start, super().send_task_start, **kwargs)

    async def send_task_success(self, **kwargs: Any) -> bool:
        return await self._rich(
            telegram_rich.format_task_success, super().send_task_success, **kwargs
        )

    async def send_task_failed(self, **kwargs: Any) -> bool:
        return await self._rich(
            telegram_rich.format_task_failed, super().send_task_failed, **kwargs
        )

    async def send_vendor_failover(self, **kwargs: Any) -> bool:
        return await self._rich(
            telegram_rich.format_vendor_failover, super().send_vendor_failover, **kwargs
        )

    async def send_budget_warning(self, **kwargs: Any) -> bool:
        return await self._rich(
            telegram_rich.format_budget_warning, super().send_budget_warning, **kwargs
        )

    async def send_budget_exceeded(self, **kwargs: Any) -> bool:
        return await self._rich(
            telegram_rich.format_budget_exceeded, super().send_budget_exceeded, **kwargs
        )

    async def send_preflight_blocked(self, **kwargs: Any) -> bool:
        return await self._rich(
            telegram_rich.format_preflight_blocked, super().send_preflight_blocked, **kwargs
        )

    async def send_operator_message(self, **kwargs: Any) -> bool:
        return await self._rich(
            telegram_rich.format_operator_message, super().send_operator_message, **kwargs
        )

    async def send_plan_approval(self, **kwargs: Any) -> bool:
        try:
            if await telegram_rich.send_plan_for_approval(
                task_key=kwargs["task_key"], task_id=kwargs["task_id"], plan=kwargs["plan"]
            ):
                return True
        except Exception:
            logger.exception("Telegram plan approval card failed — falling back")
        return await super().send_plan_approval(**kwargs)
