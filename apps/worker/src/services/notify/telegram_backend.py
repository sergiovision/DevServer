"""Telegram NotifyBackend — delivers every event through ``services.telegram``.

The base class renders each typed event as Markdown text, so this backend
only has to say whether it is configured and how to send one message.
"""

from __future__ import annotations

from config import settings
from services import telegram

from .base import NotifyBackend


class TelegramBackend(NotifyBackend):
    channel = "telegram"

    def is_configured(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    async def send_text(self, message: str) -> bool:
        return await telegram.tg_send(message)
