"""Telegram bot messaging — async notifications via Bot API or external controller.

Supports two modes controlled by TELEGRAM_CONTROLLER_MODE env var:
  - "local"    — calls Telegram Bot API directly
  - "external" — forwards requests to an external TelegramController service
"""

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


# ─── Local implementation (direct Bot API) ───────────────────────────────────

async def _local_send(message: str, parse_mode: str = "Markdown") -> bool:
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


# ─── External implementation (via TelegramController service) ────────────────

def _ext_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.telegram_controller_token:
        headers["Authorization"] = f"Bearer {settings.telegram_controller_token}"
    return headers


async def _ext_send(message: str, parse_mode: str = "Markdown") -> bool:
    url = f"{settings.telegram_controller_url}/send"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={
                "message": message,
                "parse_mode": parse_mode,
            }, headers=_ext_headers())
            if resp.status_code != 200:
                logger.warning("External TelegramController send failed: %s", resp.text)
                return False
            return True
    except Exception:
        logger.exception("External TelegramController send error")
        return False


# ─── Public API (delegates to local or external) ─────────────────────────────

def _use_external() -> bool:
    return settings.telegram_controller_mode == "external"


async def tg_send(message: str, parse_mode: str = "Markdown") -> bool:
    """Send a text message to the configured Telegram chat."""
    if _use_external():
        return await _ext_send(message, parse_mode)
    return await _local_send(message, parse_mode)
