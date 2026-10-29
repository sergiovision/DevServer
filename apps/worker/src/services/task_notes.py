"""Task Notes: short operator messages delivered to a running task.

An operator watching a task can post a note ("use the existing retry helper",
"skip the docs"). Notes are stored in ``task_messages`` with ``kind='note'``
and no sending task. The runner takes the unread notes at the start of each
attempt, puts them into that attempt's prompt and marks them read in the same
statement, so each note reaches the agent exactly once.

A note posted to a task that is not running is kept for the record but marked
read straight away: it describes a run that is already over, and replaying it
into some later run would be surprising.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Statuses in which a runner will still start another attempt.
ACTIVE_STATUSES = ("queued", "running", "verifying")
MAX_NOTE_LENGTH = 4000


class TaskNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class PostedNote:
    id: int
    #: True when a running task will see the note in its next attempt.
    will_be_delivered: bool


async def post_note(db: AsyncSession, *, task_key: str, body: str) -> PostedNote:
    """Store an operator note for ``task_key``. Commits."""
    body = body.strip()[:MAX_NOTE_LENGTH]
    if not body:
        raise ValueError("note body is empty")
    task = (
        await db.execute(
            text("SELECT id, status FROM tasks WHERE task_key = :key ORDER BY id DESC LIMIT 1"),
            {"key": task_key},
        )
    ).first()
    if task is None:
        raise TaskNotFoundError(task_key)

    deliverable = task.status in ACTIVE_STATUSES
    note_id = (
        await db.execute(
            text(
                """
                INSERT INTO task_messages (to_task_id, to_task_key, kind, body, read_at)
                VALUES (:task_id, :key, 'note', :body,
                        CASE WHEN :deliverable THEN NULL ELSE NOW() END)
                RETURNING id
                """
            ),
            {"task_id": task.id, "key": task_key, "body": body, "deliverable": deliverable},
        )
    ).scalar_one()
    await db.commit()
    return PostedNote(id=note_id, will_be_delivered=deliverable)


async def list_notes(db: AsyncSession, *, task_key: str) -> list[dict]:
    rows = await db.execute(
        text(
            """
            SELECT id, body, created_at, read_at
            FROM task_messages
            WHERE to_task_key = :key AND kind = 'note' AND from_task_id IS NULL
            ORDER BY id
            """
        ),
        {"key": task_key},
    )
    return [
        {
            "id": row.id,
            "body": row.body,
            "created_at": row.created_at.isoformat(),
            "delivered_at": row.read_at.isoformat() if row.read_at else None,
        }
        for row in rows
    ]


async def take_unread(db: AsyncSession, *, task_id: int) -> list[str]:
    """Mark the task's unread notes read and return their bodies, oldest first."""
    rows = await db.execute(
        text(
            """
            UPDATE task_messages SET read_at = NOW()
            WHERE to_task_id = :task_id AND kind = 'note'
              AND from_task_id IS NULL AND read_at IS NULL
            RETURNING id, body
            """
        ),
        {"task_id": task_id},
    )
    notes = sorted(rows.all(), key=lambda row: row.id)
    await db.commit()
    return [note.body for note in notes]


def render_notes_block(notes: list[str]) -> str:
    if not notes:
        return ""
    lines = [
        "## Operator notes",
        "The operator sent these notes while the task was running. They are the "
        "most recent human intent: follow them where they amend the task.",
    ]
    lines += [f"{number}. {note}" for number, note in enumerate(notes, 1)]
    return "\n".join(lines)
