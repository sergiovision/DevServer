"""Operator control of a task's lifecycle: cancel, and interrupt for continuation.

The HTTP routes and the Telegram bot used to carry their own near-identical
copies of "mark cancelled, fail the open runs", and none of them stopped the
agent that was actually running. Both now call this module, which updates the
database *and* cancels the in-process run through
:data:`services.running_tasks.registry`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from models.base import async_session
from services import repo_lock
from services.running_tasks import registry

#: Statuses from which a task can no longer be cancelled.
FINAL_STATUSES = ("test", "done", "cancelled", "retired")


class TaskNotFoundError(LookupError):
    pass


class TaskStateError(ValueError):
    """The task's current status does not allow the requested action."""


@dataclass(frozen=True)
class Cancelled:
    task_id: int
    task_key: str
    old_status: str
    #: True when an agent was running in this worker and has been stopped.
    stopped_running_agent: bool


async def _load(task_id: int | None, task_key: str | None) -> tuple[int, str, str]:
    if task_id is not None:
        query, params = "SELECT id, task_key, status FROM tasks WHERE id = :id", {"id": task_id}
    else:
        query = (
            "SELECT id, task_key, status FROM tasks WHERE task_key = :key ORDER BY id DESC LIMIT 1"
        )
        params = {"key": task_key}
    async with async_session() as db:
        row = (await db.execute(text(query), params)).first()
    if row is None:
        raise TaskNotFoundError(task_key or str(task_id))
    return row.id, row.task_key, row.status


async def cancel(*, task_id: int | None = None, task_key: str | None = None) -> Cancelled:
    """Cancel a task by id or key, stopping its agent if it runs here."""
    found_id, found_key, old_status = await _load(task_id, task_key)
    if old_status in FINAL_STATUSES:
        raise TaskStateError(f"Task is already {old_status}")

    async with async_session() as db:
        await db.execute(
            text("UPDATE tasks SET status = 'cancelled', updated_at = NOW() WHERE id = :id"),
            {"id": found_id},
        )
        await db.execute(
            text(
                "UPDATE task_runs SET status = 'failed', finished_at = NOW(), "
                "error_log = 'Cancelled by user' WHERE task_id = :id AND finished_at IS NULL"
            ),
            {"id": found_id},
        )
        await db.commit()
    stopped = await registry.cancel(found_id)
    return Cancelled(found_id, found_key, old_status, stopped_running_agent=stopped)


async def interrupt_for_continuation(task_id: int, task_key: str) -> None:
    """Stop a running agent so a continuation can take over its repo lock.

    The stopped run keeps its session id, so the continuation resumes it.
    """
    await registry.cancel(task_id)
    async with async_session() as db:
        await repo_lock.release_all_owned_by(db, owner=task_key)
        await db.commit()
