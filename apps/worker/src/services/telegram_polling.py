"""Telegram bot update polling — runs inside the worker process.

Uses long-polling (getUpdates) so no HTTPS certificate is required.

Supported commands:
  /status   — worker status
  /approve TASK-KEY
  /reject  TASK-KEY — cancel a pending/blocked task
  /cancel  TASK-KEY — stop a running task (alias of /reject for any status)
  /retry   TASK-KEY
  /reply   TASK-KEY <message> — send a message from the operator to a task's inbox
  /inbox   [N] — show the last N unread operator-addressed messages
  /pause
  /resume
  /mode autonomous|interactive
  /digest  — trigger daily digest now (Pro)
  /budget TASK-KEY — show budget status for a task (Pro)
  /help

Also handles callback_query updates (inline keyboard button presses)
for Pro plan approval from Telegram.
"""

import asyncio
import json
import logging
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select, update

from config import settings
from models.base import async_session
from models.setting import Setting
from models.task import Task
from services.telegram import tg_send

# Pro Telegram: conditionally import for callback handling and rich commands
try:
    from services.pro.telegram_pro import handle_plan_callback, send_daily_digest
    _has_pro_telegram = True
except ImportError:
    _has_pro_telegram = False

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


async def _cmd_cancel(task_key: str) -> str:
    """Cancel a task in ANY status — terminal or not. Complements
    /reject which only fires for pending/blocked tasks."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            return f"Task `{task_key}` not found"
        if task.status in ("done", "cancelled"):
            return f"Task is already `{task.status}`"
        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="cancelled", updated_at=datetime.now(timezone.utc))
        )
        await db.commit()
    return f"\U0001f6d1 Cancelled `{task_key}`"


async def _cmd_reply(task_key: str, body: str) -> str:
    """Send an operator-originated message to a task's inbox. The agent
    picks it up on its next inbox poll (prompted between major steps).

    Pro-only: inter-task messaging lives in ``services.pro.task_messaging``.
    In free mode this command politely declines.
    """
    try:
        from services.pro import task_messaging
    except ImportError:
        return "\u274c Inter-task messaging is a Pro feature."
    async with async_session() as db:
        try:
            msg = await task_messaging.send_message(
                db,
                from_task_key="operator",
                to_task_key=task_key,
                body=body,
                kind="note",
            )
        except ValueError as exc:
            return f"\u274c {exc}"
    excerpt = body[:60] + ("…" if len(body) > 60 else "")
    return f"\u2709 Message sent to `{task_key}` (#{msg['id']}): _{excerpt}_"


async def _cmd_inbox(n_arg: str | None) -> str:
    """Show the last N unread messages addressed to operator, without acking.

    Pro-only: the operator inbox lives on top of the pro messaging bus.
    """
    try:
        n = max(1, min(20, int(n_arg) if n_arg else 5))
    except ValueError:
        n = 5
    try:
        from services.pro import task_messaging
    except ImportError:
        return "\u274c Operator inbox is a Pro feature."
    async with async_session() as db:
        rows = await task_messaging.read_inbox(
            db, task_key="operator", mark_read=False,
            include_read=False, limit=n,
        )
    if not rows:
        return "\U0001f4ed Operator inbox is empty."
    lines = [f"\U0001f4e5 *Operator inbox* — {len(rows)} unread"]
    for r in rows:
        from_key = r.get("from_task_key") or "?"
        subj = r.get("subject") or r.get("body", "")[:60]
        lines.append(f"• `{from_key}` ({r.get('kind', '?')}): {subj}")
    lines.append("_Open /inbox in the dashboard to reply or mark read._")
    return "\n".join(lines)


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


async def _cmd_digest() -> str:
    if not _has_pro_telegram:
        return "Daily digest requires DevServer Pro."
    async with async_session() as db:
        result = await send_daily_digest(db)
    return result


async def _cmd_budget(task_key: str) -> str:
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            return f"Task `{task_key}` not found"

        max_cost = task.max_cost_usd
        max_wall = task.max_wall_seconds

        # Sum cost/duration from task_runs
        from sqlalchemy import text as sql_text
        stats = await db.execute(sql_text(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total_cost, "
            "COALESCE(SUM(duration_ms), 0) AS total_ms "
            "FROM task_runs WHERE task_id = :tid"
        ), {"tid": task.id})
        row = stats.fetchone()
        cum_cost = row[0] if row else Decimal("0")
        cum_ms = row[1] if row else 0

    wall_s = cum_ms / 1000
    lines = [
        f"\U0001f4b0 *Budget: {task_key}*",
        f"Status: `{task.status}`",
        "",
        f"Cost: ${cum_cost:.4f}" + (f" / ${max_cost:.4f}" if max_cost else " (unlimited)"),
        f"Wall: {wall_s:.0f}s" + (f" / {max_wall}s" if max_wall else " (unlimited)"),
    ]

    if max_cost and cum_cost > 0:
        pct = float(cum_cost) / float(max_cost) * 100
        lines.append(f"Cost usage: {pct:.0f}%")
    if max_wall and cum_ms > 0:
        pct = wall_s / max_wall * 100
        lines.append(f"Wall usage: {pct:.0f}%")

    return "\n".join(lines)


_HELP = (
    "*DevServer Bot Commands*\n"
    "/status — worker status\n"
    "/approve TASK\\-KEY — approve pending task\n"
    "/reject TASK\\-KEY — cancel pending/blocked task\n"
    "/cancel TASK\\-KEY — cancel task in any status\n"
    "/retry TASK\\-KEY — retry failed task\n"
    "/reply TASK\\-KEY <msg> — message the agent's inbox\n"
    "/inbox \\[N\\] — show N unread operator messages (default 5)\n"
    "/pause — pause queue\n"
    "/resume — resume queue\n"
    "/mode autonomous|interactive — set mode\n"
    "/digest — send daily digest now\n"
    "/budget TASK\\-KEY — show task budget status"
)


# ─── Dispatch ────────────────────────────────────────────────────────────────

async def _dispatch(cmd: str, args: list[str]) -> str | None:
    if cmd == "/status":
        return await _cmd_status()
    if cmd == "/approve":
        return await _cmd_approve(args[0]) if args else "Usage: /approve TASK-KEY"
    if cmd == "/reject":
        return await _cmd_reject(args[0]) if args else "Usage: /reject TASK-KEY"
    if cmd == "/cancel":
        return await _cmd_cancel(args[0]) if args else "Usage: /cancel TASK-KEY"
    if cmd == "/retry":
        return await _cmd_retry(args[0]) if args else "Usage: /retry TASK-KEY"
    if cmd == "/reply":
        if len(args) < 2:
            return "Usage: /reply TASK-KEY <message>"
        return await _cmd_reply(args[0], " ".join(args[1:]))
    if cmd == "/inbox":
        return await _cmd_inbox(args[0] if args else None)
    if cmd == "/pause":
        return await _cmd_pause()
    if cmd == "/resume":
        return await _cmd_resume()
    if cmd == "/mode":
        return await _cmd_mode(args[0]) if args else "Usage: /mode autonomous|interactive"
    if cmd == "/digest":
        return await _cmd_digest()
    if cmd == "/budget":
        return await _cmd_budget(args[0]) if args else "Usage: /budget TASK-KEY"
    if cmd == "/help":
        return _HELP
    return None


# ─── Callback query handler (inline keyboard buttons) ──────────────────────

async def _handle_callback_query(callback_query: dict) -> None:
    """Handle inline keyboard button presses (plan approve/reject)."""
    if not _has_pro_telegram:
        return

    callback_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    message = callback_query.get("message")
    message_id = message.get("message_id") if message else None

    # Validate sender
    from_user = callback_query.get("from", {})
    chat = message.get("chat", {}) if message else {}
    chat_id = str(chat.get("id", ""))
    if settings.telegram_chat_id and chat_id != settings.telegram_chat_id:
        logger.warning(
            "Ignoring callback from unauthorized chat_id=%s (user=%s)",
            chat_id, from_user.get("username", "unknown"),
        )
        return

    logger.info("Telegram callback: data=%s from=%s", data, from_user.get("username"))

    if data.startswith("plan_approve:") or data.startswith("plan_reject:"):
        try:
            await handle_plan_callback(callback_id, data, message_id)
        except Exception:
            logger.exception("Error handling plan callback %s", data)


# ─── Update handler ──────────────────────────────────────────────────────────

async def _handle_update(upd: dict) -> None:
    # Inline keyboard button press
    callback_query = upd.get("callback_query")
    if callback_query:
        await _handle_callback_query(callback_query)
        return

    # Text command
    message = upd.get("message") or upd.get("edited_message")
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
                    params={
                        "timeout": 30,
                        "offset": offset,
                        # Telegram expects allowed_updates as a JSON-serialized
                        # string, not repeat-key query params. httpx would
                        # otherwise render a Python list as
                        # ``allowed_updates=message&allowed_updates=callback_query``
                        # which Telegram silently interprets as "no filter".
                        "allowed_updates": json.dumps(["message", "callback_query"]),
                    },
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
