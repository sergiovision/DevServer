"""Inter-task messaging bus.

A running agent can leave a message for another task (supervisor → worker,
peer → peer, anyone → operator), read its own inbox, and list the live tasks
it could address. The operator talks to a task through the same bus under the
reserved ``operator`` key.

Everything flows through the ``task_messages`` table. Each send also writes a
``message_sent`` / ``message_received`` row to ``task_events`` so the
dashboard timeline picks the traffic up without extra plumbing.

Design choices:
    - ``to_task_key`` is the authoritative address; ``to_task_id`` is resolved
      at send time and kept as a hint. A message to an unknown key still
      persists with a NULL ``to_task_id`` (fire-and-forget handoffs).
    - Reading with ``mark_read=True`` is idempotent: a second call returns
      nothing new.
    - Operator notes (``services.task_notes``) share the table as
      ``kind='note'`` rows with no sender. They have their own delivery path
      (folded into the next attempt's prompt), so the bus never returns them.
    - Bodies, subjects and payloads are screened with the PR-preflight secret
      rules before they are stored, so a key pasted into a message never
      reaches another task's prompt or a notification channel.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.secret_rules import redact_secrets

logger = logging.getLogger(__name__)

#: Reserved address for the human operator. Never resolves to a task row.
OPERATOR_KEY = "operator"

#: 'request' expects a reply, 'response' is a reply, 'note' is
#: fire-and-forget, 'handoff' signals a transfer of work.
VALID_KINDS = frozenset({"request", "response", "note", "handoff"})

MAX_BODY_LENGTH = 20_000

#: Live statuses a task can be addressed in (``list_sessions``).
LIVE_STATUSES = ("pending", "queued", "running", "verifying", "blocked")

# Operator notes are stored as sender-less 'note' rows; the bus skips them.
_NOT_OPERATOR_NOTE = "NOT (kind = 'note' AND from_task_key IS NULL)"

_MESSAGE_COLUMNS = (
    "id, from_task_id, from_task_key, to_task_id, to_task_key, "
    "kind, subject, body, payload, read_at, created_at"
)


def validate(*, kind: str, body: str) -> None:
    """Raise ``ValueError`` for a message the bus must not accept."""
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind {kind!r}; expected one of {sorted(VALID_KINDS)}")
    if not body or not body.strip():
        raise ValueError("body cannot be empty")
    if len(body) > MAX_BODY_LENGTH:
        raise ValueError(f"body is longer than {MAX_BODY_LENGTH} characters")


def screen_secrets(
    body: str,
    subject: str | None,
    payload: dict | None,
) -> tuple[str, str | None, dict, list[str]]:
    """Redact secrets from every free-text part of a message.

    Returns ``(body, subject, payload, rule_keys)``. When anything was
    redacted the payload carries the sorted rule keys under ``_redacted``.
    """
    hits: list[str] = []
    body, body_hits = redact_secrets(body)
    hits.extend(body_hits)
    if subject:
        subject, subject_hits = redact_secrets(subject)
        hits.extend(subject_hits)
    safe_payload: dict = dict(payload or {})
    if safe_payload:
        try:
            redacted_json, payload_hits = redact_secrets(json.dumps(safe_payload))
            if payload_hits:
                hits.extend(payload_hits)
                safe_payload = json.loads(redacted_json)
        except TypeError, ValueError:
            logger.debug("payload secret screen skipped (not JSON-serialisable)")
    rules = sorted(set(hits))
    if rules:
        safe_payload["_redacted"] = rules
    return body, subject, safe_payload, rules


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def row_to_message(row: Any) -> dict:
    """Map a ``task_messages`` row (selected with ``_MESSAGE_COLUMNS``) to a dict."""
    return {
        "id": row[0],
        "from_task_id": row[1],
        "from_task_key": row[2],
        "to_task_id": row[3],
        "to_task_key": row[4],
        "kind": row[5],
        "subject": row[6],
        "body": row[7],
        "payload": row[8] or {},
        "read_at": _iso(row[9]),
        "created_at": _iso(row[10]),
    }


async def _task_id(session: AsyncSession, task_key: str | None) -> int | None:
    if not task_key or task_key == OPERATOR_KEY:
        return None
    row = (
        await session.execute(
            text("SELECT id FROM tasks WHERE task_key = :k ORDER BY id DESC LIMIT 1"),
            {"k": task_key},
        )
    ).fetchone()
    return row[0] if row else None


async def _emit_event(
    session: AsyncSession,
    task_id: int | None,
    event_type: str,
    payload: dict,
) -> None:
    """Best-effort task_event insert. Never raises — messaging must not abort a task."""
    if task_id is None:
        return
    try:
        await session.execute(
            text(
                "INSERT INTO task_events (task_id, event_type, payload) "
                "VALUES (:tid, :et, CAST(:pl AS JSONB))"
            ),
            {"tid": task_id, "et": event_type, "pl": json.dumps(payload)},
        )
        await session.commit()
    except Exception:
        logger.exception("task_messaging event emit failed")
        await session.rollback()


async def _notify_operator(
    *, from_task_key: str | None, subject: str | None, body: str, kind: str
) -> None:
    """Fan an operator-addressed message out to every notification channel."""
    try:
        from services.notify import notify

        await notify.operator_message(
            from_task_key=from_task_key or "unknown",
            subject=subject,
            body=body,
            kind=kind,
        )
    except Exception:
        logger.exception("operator_message fan-out failed")


async def send_message(
    session: AsyncSession,
    *,
    from_task_key: str | None,
    to_task_key: str,
    body: str,
    subject: str | None = None,
    kind: str = "note",
    payload: dict | None = None,
) -> dict:
    """Store a message, emit timeline events and return it. Commits.

    Raises ``ValueError`` for an invalid kind, empty body or missing address.
    """
    to_task_key = (to_task_key or "").strip()
    if not to_task_key:
        raise ValueError("to_task_key is required")
    validate(kind=kind, body=body)
    body, subject, safe_payload, redacted = screen_secrets(body, subject, payload)

    from_task_id = await _task_id(session, from_task_key)
    to_task_id = await _task_id(session, to_task_key)

    row = (
        await session.execute(
            text(
                """
                INSERT INTO task_messages
                    (from_task_id, from_task_key, to_task_id, to_task_key,
                     kind, subject, body, payload)
                VALUES
                    (:fid, :fkey, :tid, :tkey, :kind, :subj, :body, CAST(:pl AS JSONB))
                RETURNING id, created_at
                """
            ),
            {
                "fid": from_task_id,
                "fkey": from_task_key,
                "tid": to_task_id,
                "tkey": to_task_key,
                "kind": kind,
                "subj": subject,
                "body": body,
                "pl": json.dumps(safe_payload),
            },
        )
    ).fetchone()
    await session.commit()
    if row is None:  # INSERT … RETURNING always yields a row; keeps pyright honest
        raise RuntimeError("task_messages insert returned no row")
    msg_id, created_at = row[0], row[1]

    if redacted:
        logger.warning(
            "Redacted %d secret rule(s) from message %s (%s)",
            len(redacted),
            msg_id,
            ",".join(redacted),
        )
        await _emit_event(
            session,
            from_task_id,
            "message_redacted",
            {"message_id": msg_id, "to": to_task_key, "rules": redacted},
        )

    event_payload = {
        "message_id": msg_id,
        "from": from_task_key,
        "to": to_task_key,
        "kind": kind,
        "subject": subject or body[:80],
    }
    await _emit_event(session, from_task_id, "message_sent", event_payload)
    await _emit_event(session, to_task_id, "message_received", event_payload)

    if to_task_key == OPERATOR_KEY:
        await _notify_operator(from_task_key=from_task_key, subject=subject, body=body, kind=kind)

    return {
        "id": msg_id,
        "from_task_id": from_task_id,
        "from_task_key": from_task_key,
        "to_task_id": to_task_id,
        "to_task_key": to_task_key,
        "kind": kind,
        "subject": subject,
        "body": body,
        "payload": safe_payload,
        "read_at": None,
        "created_at": _iso(created_at),
    }


async def read_inbox(
    session: AsyncSession,
    *,
    task_key: str,
    mark_read: bool = True,
    limit: int = 20,
    include_read: bool = False,
    newest_first: bool = False,
) -> list[dict]:
    """Return up to ``limit`` messages addressed to ``task_key``, oldest first.

    With ``mark_read=True`` the unread ones are stamped read and will not come
    back. ``include_read=True`` returns the transcript regardless of state.
    ``newest_first=True`` returns the newest ``limit`` instead (Telegram
    ``/inbox``).
    """
    task_id = await _task_id(session, task_key)
    where = ["to_task_key = :tkey", _NOT_OPERATOR_NOTE]
    params: dict[str, Any] = {"tkey": task_key, "lim": max(1, min(limit, 200))}
    if task_id is not None:
        where.append("(to_task_id = :tid OR to_task_id IS NULL)")
        params["tid"] = task_id
    if not include_read:
        where.append("read_at IS NULL")
    order = "DESC" if newest_first else "ASC"

    rows = (
        await session.execute(
            text(
                f"SELECT {_MESSAGE_COLUMNS} FROM task_messages "
                f"WHERE {' AND '.join(where)} ORDER BY id {order} LIMIT :lim"
            ),
            params,
        )
    ).fetchall()
    messages = [row_to_message(r) for r in rows]

    unread_ids = [m["id"] for m in messages if m["read_at"] is None]
    if mark_read and unread_ids:
        await session.execute(
            text("UPDATE task_messages SET read_at = NOW() WHERE id = ANY(:ids)"),
            {"ids": unread_ids},
        )
        await session.commit()
        await _emit_event(
            session,
            task_id,
            "message_received",
            {"count": len(unread_ids), "message_ids": unread_ids, "ack": True},
        )
    return messages


async def thread(session: AsyncSession, *, task_key: str, limit: int = 50) -> list[dict]:
    """The task's conversation in both directions, oldest first (newest ``limit``)."""
    rows = (
        await session.execute(
            text(
                f"SELECT {_MESSAGE_COLUMNS} FROM task_messages "
                f"WHERE (from_task_key = :k OR to_task_key = :k) AND {_NOT_OPERATOR_NOTE} "
                "ORDER BY id DESC LIMIT :lim"
            ),
            {"k": task_key, "lim": max(1, min(limit, 500))},
        )
    ).fetchall()
    return [row_to_message(r) for r in reversed(rows)]


async def list_sessions(session: AsyncSession, *, limit: int = 50) -> list[dict]:
    """Live tasks that can receive messages, most urgent first."""
    rows = (
        await session.execute(
            text(
                """
                SELECT t.task_key, t.title, t.status, t.priority, r.name AS repo_name,
                       t.agent_vendor, t.claude_model, t.updated_at
                FROM tasks t
                LEFT JOIN repos r ON r.id = t.repo_id
                WHERE t.status = ANY(:statuses)
                ORDER BY t.priority ASC, t.updated_at DESC
                LIMIT :lim
                """
            ),
            {"statuses": list(LIVE_STATUSES), "lim": max(1, min(limit, 200))},
        )
    ).fetchall()
    return [
        {
            "task_key": r[0],
            "title": r[1],
            "status": r[2],
            "priority": r[3],
            "repo_name": r[4],
            "vendor": r[5],
            "model": r[6],
            "updated_at": _iso(r[7]),
        }
        for r in rows
    ]
