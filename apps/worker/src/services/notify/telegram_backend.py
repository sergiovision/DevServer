"""Telegram NotifyBackend — delegates to the existing services/telegram.py
and, when Pro is installed, to services/pro/telegram_pro for rich
notifications with inline keyboards.

This lets the dispatcher treat Telegram like any other channel while
preserving the hand-tuned Markdown formatting that operators already
recognise.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from config import settings
from services import telegram

try:
    from services.pro import telegram_pro as _tg_pro
    _HAS_PRO = True
except ImportError:
    _tg_pro = None  # type: ignore
    _HAS_PRO = False

from .base import NotifyBackend

logger = logging.getLogger(__name__)


class TelegramBackend(NotifyBackend):
    channel = "telegram"

    def is_configured(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    async def send_text(self, message: str) -> bool:
        return await telegram.tg_send(message)

    # Override each event to use the rich pro formatter when available —
    # falls back to plain tg_send otherwise so the free build still gets
    # perfectly readable notifications.

    async def send_task_start(self, **kwargs) -> bool:
        if _HAS_PRO and _tg_pro is not None:
            try:
                return bool(await _tg_pro.send_task_start(
                    kwargs["task_key"], kwargs["title"], kwargs["repo_name"],
                    kwargs["mode"], kwargs["vendor"], kwargs["model"],
                ))
            except Exception:
                logger.exception("telegram_pro.send_task_start failed — falling back to text")
        return await super().send_task_start(**kwargs)

    async def send_task_success(self, **kwargs) -> bool:
        if _HAS_PRO and _tg_pro is not None:
            try:
                await _tg_pro.send_task_success(
                    kwargs["task_key"], kwargs["git_flow"], kwargs.get("pr_url"),
                    kwargs["attempts"], kwargs["turns"], kwargs["cost"],
                    kwargs["duration_ms"], kwargs["repo_name"],
                )
                return True
            except Exception:
                logger.exception("telegram_pro.send_task_success failed — falling back")
        return await super().send_task_success(**kwargs)

    async def send_task_failed(self, **kwargs) -> bool:
        if _HAS_PRO and _tg_pro is not None:
            try:
                return bool(await _tg_pro.send_task_failed(
                    kwargs["task_key"], kwargs["repo_name"],
                    kwargs["error_context"], kwargs["attempts"], kwargs["cost"],
                ))
            except Exception:
                logger.exception("telegram_pro.send_task_failed failed — falling back")
        return await super().send_task_failed(**kwargs)

    async def send_vendor_failover(self, **kwargs) -> bool:
        if _HAS_PRO and _tg_pro is not None:
            try:
                return bool(await _tg_pro.send_vendor_failover(
                    kwargs["task_key"], kwargs["repo_name"],
                    kwargs["from_vendor"], kwargs["from_model"],
                    kwargs["to_vendor"], kwargs["to_model"],
                ))
            except Exception:
                logger.exception("telegram_pro.send_vendor_failover failed — falling back")
        return await super().send_vendor_failover(**kwargs)

    async def send_budget_warning(self, **kwargs) -> bool:
        if _HAS_PRO and _tg_pro is not None:
            try:
                return bool(await _tg_pro.send_budget_warning(
                    kwargs["task_key"], kwargs["repo_name"], kwargs["reason"],
                    kwargs["cum_cost"], kwargs["cum_wall_ms"],
                    kwargs.get("max_cost"), kwargs.get("max_wall"),
                ))
            except Exception:
                logger.exception("telegram_pro.send_budget_warning failed — falling back")
        return await super().send_budget_warning(**kwargs)

    async def send_budget_exceeded(self, **kwargs) -> bool:
        if _HAS_PRO and _tg_pro is not None:
            try:
                return bool(await _tg_pro.send_budget_exceeded(
                    kwargs["task_key"], kwargs["repo_name"], kwargs["reason"],
                    kwargs["cum_cost"], kwargs["cum_wall_ms"],
                ))
            except Exception:
                logger.exception("telegram_pro.send_budget_exceeded failed — falling back")
        return await super().send_budget_exceeded(**kwargs)

    async def send_preflight_blocked(self, **kwargs) -> bool:
        if _HAS_PRO and _tg_pro is not None:
            try:
                return bool(await _tg_pro.send_preflight_blocked(
                    kwargs["task_key"], kwargs["violations"],
                ))
            except Exception:
                logger.exception("telegram_pro.send_preflight_blocked failed — falling back")
        return await super().send_preflight_blocked(**kwargs)
