"""Telegram bot update polling — runs inside the worker process.

Activated only when TELEGRAM_CONTROLLER_MODE=local.
Uses long-polling (getUpdates) so no HTTPS certificate is required.

Supported commands:
  /status   — worker status
  /approve TASK-KEY
  /reject  TASK-KEY
  /retry   TASK-KEY
  /pause
  /resume
  /mode autonomous|interactive
  /help
"""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

from config import settings
from models.base import async_session
from models.setting import Setting
from models.task import Task
from services.telegram import tg_send

logger = logging.getLogger(__name__)

_poll_task: asyncio.Task | None = None


# ─── Command handlers (direct DB, no HTTP round-trip) ────────────────────────

async def _cmd_status() -> str:
    from services.queue_consumer import is_consumer_running

    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.status.in_(["running", "verifying"])))
        active = res.scalars().all()
        res2 = await db.execute(
            select(Task).where(Task.status.in_(["pending", "queued"])).order_by(Task.priority)
        )
        queued = res2.scalars().all()
        res3 = await db.execute(select(Setting))
        s = {row.key: row.value for row in res3.scalars().all()}

    import json as _json
    mode = s.get("mode", "autonomous")
    if isinstance(mode, str) and mode.startswith('"'):
        mode = _json.loads(mode)
    paused = s.get("paused", False)
    if isinstance(paused, str):
        paused = _json.loads(paused)

    worker = "running" if is_consumer_running() else "stopped"
    active_keys = ", ".join(f"`{t.task_key}`" for t in active) or "none"
    queued_keys = ", ".join(f"`{t.task_key}`" for t in queued) or "none"
    return (
        f"*DevServer Status*\n"
        f"Mode: `{mode}` | Paused: `{paused}` | Worker: `{worker}`\n"
        f"Active ({len(active)}): {active_keys}\n"
        f"Queued ({len(queued)}): {queued_keys}"
    )


async def _cmd_approve(task_key: str) -> str:
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            return f"Task `{task_key}` not found"
        if task.status not in ("pending", "blocked"):
            return f"Task is `{task.status}`, cannot approve"
        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="queued", updated_at=datetime.now(timezone.utc))
        )
        await db.commit()
    return f"\u2705 Approved `{task_key}`"


async def _cmd_reject(task_key: str) -> str:
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            return f"Task `{task_key}` not found"
        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="cancelled", updated_at=datetime.now(timezone.utc))
        )
        await db.commit()
    return f"\U0001f6ab Rejected `{task_key}`"


async def _cmd_retry(task_key: str) -> str:
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            return f"Task `{task_key}` not found"
        if task.status not in ("failed", "cancelled"):
            return f"Task is `{task.status}`, can only retry failed/cancelled"
        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="queued", updated_at=datetime.now(timezone.utc))
        )
        await db.commit()
    return f"\U0001f504 Re-queued `{task_key}`"


async def _cmd_pause() -> str:
    async with async_session() as db:
        setting = await db.get(Setting, "paused")
        if setting:
            setting.value = True
            setting.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key="paused", value=True))
        await db.commit()
    return "\u23f8 Dispatching paused"


async def _cmd_resume() -> str:
    async with async_session() as db:
        setting = await db.get(Setting, "paused")
        if setting:
            setting.value = False
            setting.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key="paused", value=False))
        await db.commit()
    return "\u25b6\ufe0f Dispatching resumed"


async def _cmd_mode(mode_arg: str) -> str:
    mode = mode_arg.lower()
    if mode == "auto":
        mode = "autonomous"
    if mode not in ("autonomous", "interactive"):
        return "Usage: /mode autonomous|interactive"
    async with async_session() as db:
        setting = await db.get(Setting, "mode")
        if setting:
            setting.value = mode
            setting.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key="mode", value=mode))
        await db.commit()
    return f"Mode set to `{mode}`"


_HELP = (
    "*DevServer Bot Commands*\n"
    "/status — worker status\n"
    "/approve TASK\\-KEY — approve pending task\n"
    "/reject TASK\\-KEY — cancel task\n"
    "/retry TASK\\-KEY — retry failed task\n"
    "/pause — pause queue\n"
    "/resume — resume queue\n"
    "/mode autonomous|interactive — set mode"
)


# ─── Dispatch ────────────────────────────────────────────────────────────────

async def _dispatch(cmd: str, args: list[str]) -> str | None:
    if cmd == "/status":
        return await _cmd_status()
    if cmd == "/approve":
        return await _cmd_approve(args[0]) if args else "Usage: /approve TASK-KEY"
    if cmd == "/reject":
        return await _cmd_reject(args[0]) if args else "Usage: /reject TASK-KEY"
    if cmd == "/retry":
        return await _cmd_retry(args[0]) if args else "Usage: /retry TASK-KEY"
    if cmd == "/pause":
        return await _cmd_pause()
    if cmd == "/resume":
        return await _cmd_resume()
    if cmd == "/mode":
        return await _cmd_mode(args[0]) if args else "Usage: /mode autonomous|interactive"
    if cmd == "/help":
        return _HELP
    return None


# ─── Update handler ──────────────────────────────────────────────────────────

async def _handle_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    chat_id = str(message["chat"]["id"])
    if settings.telegram_chat_id and chat_id != settings.telegram_chat_id:
        logger.warning("Ignoring command from unauthorized chat_id=%s", chat_id)
        return

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    logger.info("Telegram command: %s %s", cmd, args)
    try:
        reply = await _dispatch(cmd, args)
    except Exception:
        logger.exception("Error handling Telegram command %s", cmd)
        reply = f"Error executing `{cmd}` — check worker logs"

    if reply:
        await tg_send(reply)


# ─── Polling loop ────────────────────────────────────────────────────────────

async def _poll() -> None:
    base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    offset = 0
    logger.info("Telegram long-polling started")

    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                resp = await client.get(
                    f"{base_url}/getUpdates",
                    params={"timeout": 30, "offset": offset, "allowed_updates": ["message"]},
                )
            data = resp.json()
            if not data.get("ok"):
                logger.warning("getUpdates not-ok: %s", data)
                await asyncio.sleep(5)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                await _handle_update(upd)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Telegram poll error — retrying in 5s")
            await asyncio.sleep(5)

    logger.info("Telegram long-polling stopped")


# ─── Public start/stop ───────────────────────────────────────────────────────

def start_polling() -> None:
    """Start the background polling task. No-op if token not configured."""
    global _poll_task
    if settings.telegram_controller_mode != "local":
        return
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — polling disabled")
        return
    _poll_task = asyncio.create_task(_poll())


async def stop_polling() -> None:
    """Cancel and await the polling task."""
    global _poll_task
    if _poll_task is None:
        return
    _poll_task.cancel()
    with suppress(asyncio.CancelledError):
        await _poll_task
    _poll_task = None
