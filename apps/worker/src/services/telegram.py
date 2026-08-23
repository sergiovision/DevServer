"""Telegram bot messaging — sends notifications via the Bot API directly."""

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


def _tag(message: str) -> str:
    """Prefix a message with this deployment's name, in fleet setups only.

    Unlike getUpdates, sendMessage is not exclusive per bot token, so any
    number of instances can report into one chat — but the messages are then
    indistinguishable. Setting INSTANCE_NAME explicitly opts into a `[name]`
    prefix; leaving it blank keeps single-instance output exactly as before.
    """
    name = settings.instance_name.strip()
    if not name:
        return message
    return f"`[{name}]` {message}"


async def tg_send(message: str, parse_mode: str = "Markdown") -> bool:
    """Send a text message to the configured Telegram chat."""
    if not settings.telegram_bot_token:
        logger.debug("Telegram token not configured, skipping message")
        return False

    message = _tag(message)

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
