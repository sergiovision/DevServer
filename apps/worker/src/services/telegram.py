"""Telegram bot messaging — sends notifications via the Bot API directly."""

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


async def tg_send(message: str, parse_mode: str = "Markdown") -> bool:
    """Send a text message to the configured Telegram chat."""
    if not settings.telegram_bot_token:
        logger.debug("Telegram token not configured, skipping message")
        return False

    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, data={
                "chat_id": settings.telegram_chat_id,
                "parse_mode": parse_mode,
                "text": message,
            })
            if resp.status_code != 200:
                logger.warning("Telegram sendMessage failed: %s", resp.text)
                return False
            return True
    except Exception:
        logger.exception("Telegram send error")
        return False
