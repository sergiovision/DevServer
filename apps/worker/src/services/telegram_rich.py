"""Rich Telegram notifications: formatted task cards and plan approval buttons.

``notify.telegram_backend`` renders every typed event through the ``format_*``
functions here instead of the plain-text defaults other channels get. Plan
approvals go out with inline Approve / Reject buttons; ``telegram_polling``
routes the button presses to ``handle_plan_callback``, which sets the same
``plan_approved_at`` / ``plan_rejected_at`` columns the dashboard does and
``plan_gate`` polls.

The ``format_*`` functions are pure so they can be unit-tested without a bot.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text

from config import settings
from services.telegram import TELEGRAM_API, _tag

logger = logging.getLogger(__name__)

PLAN_APPROVE = "plan_approve"
PLAN_REJECT = "plan_reject"

_GIT_FLOW_ICONS = {
    "branch": "\U0001f517",
    "commit": "\U0001f4be",
    "patch": "\U0001f4e6",
    "untracked": "\U0001f4c2",
}


# ─── Formatting ─────────────────────────────────────────────────────────────

# Telegram legacy Markdown: outside an entity, ``_ * ` [`` are escaped with a
# backslash; inside one nothing can be escaped, so the entity's own delimiter
# is dropped from the value. Without this, one ``_`` in a title or model name
# makes Telegram reject the whole message ("can't parse entities").
_MD_SPECIAL = re.compile(r"([_*`\[])")


def _md(value: object) -> str:
    """Escape a value placed in plain (non-entity) Markdown text."""
    return _MD_SPECIAL.sub(r"\\\1", str(value))


def _in(value: object, delim: str) -> str:
    """Make a value safe inside a ``delim``-delimited entity (``*``, ``_``, `` ` ``)."""
    return str(value).replace(delim, "")


def format_task_start(
    *, task_key: str, title: str, repo_name: str, mode: str, vendor: str, model: str
) -> str:
    return "\n".join(
        [
            f"\U0001f527 *Starting: {_in(task_key, '*')}*",
            f"_{_in(title, '_')}_",
            "",
            f"\U0001f4e6 Repo: `{_in(repo_name, '`')}`",
            f"\U0001f916 Agent: {_md(vendor)}/{_md(model)}",
            f"\U0001f3af Mode: {_md(mode)}",
        ]
    )


def _git_flow_line(git_flow: str, pr_url: str | None) -> str:
    icon = _GIT_FLOW_ICONS.get(git_flow, "")
    if git_flow == "branch":
        return f"{icon} PR: {_md(pr_url) if pr_url else 'push failed'}"
    if git_flow == "commit":
        return f"{icon} Committed directly"
    if git_flow == "patch":
        return f"{icon} Patch generated"
    if git_flow == "untracked":
        return f"{icon} Untracked changes left in local folder"
    return f"Git flow: {_md(git_flow)}" if git_flow else ""


def format_task_success(
    *,
    task_key: str,
    git_flow: str,
    pr_url: str | None,
    attempts: int,
    turns: int,
    cost: Decimal,
    duration_ms: int,
    repo_name: str,
) -> str:
    duration_min = (duration_ms or 0) / 1000 / 60
    lines = [f"✅ *{_in(task_key, '*')}* completed", ""]
    flow = _git_flow_line(git_flow, pr_url)
    if flow:
        lines += [flow, ""]
    lines += [
        f"⏱ Duration: {duration_min:.1f}m",
        f"\U0001f504 Attempts: {attempts} | Turns: {turns}",
        f"\U0001f4b0 Cost: ${cost:.4f}",
        f"\U0001f4e6 Repo: `{_in(repo_name, '`')}`",
    ]
    return "\n".join(lines)


def format_task_failed(
    *, task_key: str, repo_name: str, error_context: str, attempts: int, cost: Decimal
) -> str:
    excerpt = (error_context or "").strip()[:400].replace("```", "'''")
    lines = [
        f"❌ *{_in(task_key, '*')}* FAILED",
        "",
        f"\U0001f4e6 Repo: `{_in(repo_name, '`')}`",
        f"\U0001f504 Attempts: {attempts} | Cost: ${cost:.4f}",
    ]
    if excerpt:
        lines += ["", f"*Error:*\n```\n{excerpt}\n```"]
    return "\n".join(lines)


def format_vendor_failover(
    *,
    task_key: str,
    repo_name: str,
    from_vendor: str,
    from_model: str,
    to_vendor: str,
    to_model: str,
) -> str:
    return "\n".join(
        [
            f"\U0001f504 *Vendor Failover: {_in(task_key, '*')}*",
            "",
            f"\U0001f4e6 Repo: `{_in(repo_name, '`')}`",
            f"❌ Primary: {_md(from_vendor)}/{_md(from_model)}",
            f"✅ Failover: {_md(to_vendor)}/{_md(to_model)}",
            "",
            "_Primary vendor exhausted all retries — switching to the backup vendor._",
        ]
    )


def format_budget_warning(
    *,
    task_key: str,
    repo_name: str,
    reason: str,
    cum_cost: Decimal,
    cum_wall_ms: int,
    max_cost: Decimal | None,
    max_wall: int | None,
) -> str:
    wall_s = (cum_wall_ms or 0) / 1000
    cost_line = f"\U0001f4b0 Cost: ${cum_cost:.4f}" + (f" / ${max_cost:.4f}" if max_cost else "")
    wall_line = f"⏱ Wall: {wall_s:.0f}s" + (f" / {max_wall}s" if max_wall else "")
    return "\n".join(
        [
            f"⚠️ *Budget Warning: {_in(task_key, '*')}*",
            "",
            f"\U0001f4e6 Repo: `{_in(repo_name, '`')}`",
            cost_line,
            wall_line,
            "",
            f"_{_in(reason, '_')}_",
        ]
    )


def format_budget_exceeded(
    *, task_key: str, repo_name: str, reason: str, cum_cost: Decimal, cum_wall_ms: int
) -> str:
    wall_s = (cum_wall_ms or 0) / 1000
    return "\n".join(
        [
            f"\U0001f6d1 *Budget Exceeded: {_in(task_key, '*')}*",
            "",
            f"\U0001f4e6 Repo: `{_in(repo_name, '`')}`",
            f"\U0001f4b0 Cost: ${cum_cost:.4f} | Wall: {wall_s:.0f}s",
            f"_{_in(reason, '_')}_",
            "",
            "_Task has been blocked. Increase the budget or continue manually._",
        ]
    )


def format_preflight_blocked(*, task_key: str, violations: list[dict]) -> str:
    lines = [f"\U0001f6d1 *Preflight Blocked: {_in(task_key, '*')}*", ""]
    for v in violations[:5]:
        icon = "\U0001f534" if v.get("severity", "hard") == "hard" else "\U0001f7e1"
        lines.append(
            f"{icon} *{_in(v.get('kind', 'unknown'), '*')}*: {_md((v.get('detail') or '')[:150])}"
        )
    if len(violations) > 5:
        lines.append(f"_...and {len(violations) - 5} more violations_")
    return "\n".join(lines)


def format_operator_message(
    *, from_task_key: str, subject: str | None, body: str, kind: str
) -> str:
    excerpt = body.strip()
    if len(excerpt) > 600:
        excerpt = excerpt[:600] + "…"
    lines = [f"✉️ *Message from {_in(from_task_key, '*')}* ({_md(kind)})"]
    if subject:
        lines.append(f"*{_in(subject[:200], '*')}*")
    lines += ["", _md(excerpt), "", f"_Reply with_ `/reply {_in(from_task_key, '`')} <message>`"]
    return "\n".join(lines)


def format_plan_card(*, task_key: str, plan: dict) -> str:
    summary = str(plan.get("summary") or "(no summary)")
    files = list(plan.get("files_to_touch") or [])
    steps = list(plan.get("steps") or [])
    lines = [f"\U0001f4cb *Plan: {_in(task_key, '*')}*", "", f"*Summary:* {_md(summary[:300])}", ""]
    if files:
        lines.append(f"*Files ({len(files)}):*")
        lines += [f"  • `{_in(f, '`')}`" for f in files[:10]]
        if len(files) > 10:
            lines.append(f"  _...and {len(files) - 10} more_")
        lines.append("")
    if steps:
        lines.append(f"*Steps ({len(steps)}):*")
        for i, step in enumerate(steps[:6], 1):
            step_text = step if isinstance(step, str) else step.get("description", str(step))
            lines.append(f"  {i}. {_md(str(step_text)[:100])}")
        if len(steps) > 6:
            lines.append(f"  _...and {len(steps) - 6} more_")
        lines.append("")
    lines.append("_Approve or reject this plan:_")
    return "\n".join(lines)


def plan_keyboard(task_id: int, run_id: int) -> list[list[dict]]:
    """Approve / Reject buttons bound to one plan run, so a stale card cannot
    decide a later plan of the same task."""
    return [
        [
            {"text": "✅ Approve", "callback_data": f"{PLAN_APPROVE}:{task_id}:{run_id}"},
            {"text": "❌ Reject", "callback_data": f"{PLAN_REJECT}:{task_id}:{run_id}"},
        ]
    ]


def parse_plan_callback(data: str) -> tuple[str, int, int | None] | None:
    """``"plan_approve:42:7"`` → ``("approve", 42, 7)``; None when it is not a plan button.

    A legacy two-part ``"plan_approve:42"`` (sent before buttons carried the
    run id) parses with ``run_id=None`` and is refused as outdated.
    """
    action, sep, rest = data.partition(":")
    if not sep or action not in (PLAN_APPROVE, PLAN_REJECT):
        return None
    raw_id, _, raw_run = rest.partition(":")
    try:
        task_id = int(raw_id)
        run_id = int(raw_run) if raw_run else None
    except ValueError:
        return None
    return ("approve" if action == PLAN_APPROVE else "reject"), task_id, run_id


# ─── Bot API calls ──────────────────────────────────────────────────────────


async def _bot_call(method: str, data: dict[str, Any]) -> dict | None:
    if not settings.telegram_bot_token:
        return None
    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/{method}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, data=data)
        if resp.status_code != 200:
            logger.warning("Telegram %s failed: %s", method, resp.text)
            return None
        return resp.json()
    except Exception:
        logger.exception("Telegram %s error", method)
        return None


async def send_with_keyboard(
    message: str,
    keyboard: list[list[dict]] | None = None,
    parse_mode: str = "Markdown",
) -> int | None:
    """Send a message with an optional inline keyboard. Returns its message_id."""
    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "parse_mode": parse_mode,
        "text": _tag(message),
    }
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    data = await _bot_call("sendMessage", payload)
    if not data and parse_mode:
        # Most likely "can't parse entities": resend as plain text, keeping
        # the buttons, rather than losing the card.
        payload.pop("parse_mode", None)
        data = await _bot_call("sendMessage", payload)
    if not data:
        return None
    return (data.get("result") or {}).get("message_id")


async def answer_callback(callback_query_id: str, text_msg: str = "") -> None:
    """Dismiss the button's loading spinner, optionally with a toast."""
    await _bot_call(
        "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text_msg}
    )


async def edit_message(message_id: int, new_text: str, parse_mode: str = "Markdown") -> None:
    """Replace a sent message's text (drops its inline keyboard)."""
    await _bot_call(
        "editMessageText",
        {
            "chat_id": settings.telegram_chat_id,
            "message_id": message_id,
            "text": _tag(new_text),
            "parse_mode": parse_mode,
        },
    )


async def _pending_plan_run_id(task_id: int) -> int | None:
    """The run holding the task's current plan (the newest with ``plan_json``)."""
    from models.base import async_session

    async with async_session() as db:
        row = (
            await db.execute(
                text(
                    "SELECT id FROM task_runs WHERE task_id = :id AND plan_json IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"id": task_id},
            )
        ).fetchone()
    return int(row[0]) if row else None


async def send_plan_for_approval(*, task_key: str, task_id: int, plan: dict) -> bool:
    """Post the plan card with Approve / Reject buttons bound to the plan's run."""
    run_id = await _pending_plan_run_id(task_id)
    if run_id is None:
        return False  # caller falls back to the plain-text notice
    message_id = await send_with_keyboard(
        format_plan_card(task_key=task_key, plan=plan), plan_keyboard(task_id, run_id)
    )
    return message_id is not None


async def handle_plan_callback(
    callback_query_id: str, callback_data: str, message_id: int | None
) -> None:
    """Apply an Approve / Reject button press to the task's plan gate.

    Mirrors ``decidePlan`` in ``apps/web/src/lib/task-steering.ts`` (the
    dashboard's transition): interactive task, not yet decided, then set
    ``plan_approved_at`` / ``plan_rejected_at`` and ``updated_at``. Keep the two
    in step. In addition the button must belong to the task's current plan run
    (a stale card from an earlier plan is refused), and the UPDATE re-checks
    "undecided" atomically so two concurrent presses cannot both win.
    """
    from models.base import async_session

    parsed = parse_plan_callback(callback_data)
    if parsed is None:
        await answer_callback(callback_query_id, "Invalid action")
        return
    action, task_id, run_id = parsed
    if run_id is None:
        await answer_callback(callback_query_id, "Outdated button — decide from the dashboard")
        return

    column = "plan_approved_at" if action == "approve" else "plan_rejected_at"
    now = datetime.now(UTC)
    async with async_session() as db:
        row = (
            await db.execute(
                text(
                    "SELECT task_key, mode, plan_approved_at, plan_rejected_at "
                    "FROM tasks WHERE id = :id"
                ),
                {"id": task_id},
            )
        ).fetchone()
        if row is None:
            await answer_callback(callback_query_id, "Task not found")
            return
        task_key, mode, approved_at, rejected_at = row
        if mode != "interactive":
            await answer_callback(callback_query_id, f"{task_key} is not interactive")
            return
        if approved_at or rejected_at:
            await answer_callback(
                callback_query_id, f"Already {'approved' if approved_at else 'rejected'}"
            )
            return

        current_run = (
            await db.execute(
                text(
                    "SELECT id FROM task_runs WHERE task_id = :id AND plan_json IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"id": task_id},
            )
        ).fetchone()
        if current_run is None or int(current_run[0]) != run_id:
            await answer_callback(callback_query_id, "This plan was superseded — button is stale")
            return

        result = await db.execute(
            text(
                f"UPDATE tasks SET {column} = :ts, updated_at = :ts "
                "WHERE id = :id AND mode = 'interactive' "
                "AND plan_approved_at IS NULL AND plan_rejected_at IS NULL"
            ),
            {"ts": now, "id": task_id},
        )
        await db.commit()
        if getattr(result, "rowcount", 0) != 1:
            await answer_callback(callback_query_id, "Already decided")
            return

    verb = "approved" if action == "approve" else "rejected"
    await answer_callback(callback_query_id, f"{verb.capitalize()} {task_key}")
    if message_id:
        icon = "✅" if action == "approve" else "❌"
        await edit_message(
            message_id,
            f"{icon} *Plan {verb}: {_in(task_key, '*')}*\n"
            f"_{verb.capitalize()} at {now.strftime('%H:%M UTC')}_",
        )
