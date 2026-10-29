"""Database operations used by the runner, one short session each.

The runner spends most of its time waiting on subprocesses. Holding a
database session across those waits pins a pooled connection for up to an
hour per task, so every operation here opens its own session, does one unit
of work and commits.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text, update

from models.base import async_session
from models.daily_stat import DailyStat
from models.task import Task
from models.task_event import TaskEvent
from models.task_run import TaskRun
from services import telemetry


async def emit_event(task_id: int, run_id: int | None, event_type: str, payload: dict) -> None:
    """Insert a task_event row (a DB trigger relays it to the dashboard)."""
    async with async_session() as db:
        db.add(TaskEvent(task_id=task_id, run_id=run_id, event_type=event_type, payload=payload))
        await db.commit()
    # Single choke point, so every event also lands on the OTel span.
    telemetry.record_event(task_id, event_type, payload)


async def set_task_status(task_id: int, status: str) -> None:
    async with async_session() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status=status, updated_at=datetime.now(UTC))
        )
        await db.commit()


async def set_status_unless_moved_on(task_id: int, status: str) -> None:
    """Set ``status`` only while the task is still in an active state.

    Used on cancellation: an operator's Cancel or Continue may already have
    written the task's next status, which must win over the runner's.
    """
    async with async_session() as db:
        await db.execute(
            text(
                "UPDATE tasks SET status = :status, updated_at = NOW() "
                "WHERE id = :id AND status IN ('queued', 'running', 'verifying')"
            ),
            {"status": status, "id": task_id},
        )
        await db.commit()


async def set_task_columns(task_id: int, **values: Any) -> None:
    async with async_session() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(**values))
        await db.commit()


async def consume_continuation(task_id: int) -> str | None:
    """Clear the task's continuation flag; return the session to resume."""
    async with async_session() as db:
        session_id = (
            await db.execute(
                text(
                    "SELECT session_id FROM task_runs "
                    "WHERE task_id = :tid AND session_id IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"tid": task_id},
            )
        ).scalar_one_or_none()
        await db.execute(update(Task).where(Task.id == task_id).values(is_continuation=False))
        await db.commit()
    return session_id


async def start_run(task_id: int, *, attempt: int, branch: str) -> int:
    async with async_session() as db:
        run = TaskRun(task_id=task_id, attempt=attempt, branch=branch, status="started")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def update_run(run_id: int, **values: Any) -> None:
    async with async_session() as db:
        await db.execute(update(TaskRun).where(TaskRun.id == run_id).values(**values))
        await db.commit()


async def finish_run(run_id: int, *, status: str, **values: Any) -> None:
    await update_run(run_id, status=status, finished_at=datetime.now(UTC), **values)


async def fail_open_runs(task_id: int, *, reason: str) -> None:
    """Mark every unfinished run of the task failed (cancel / crash paths)."""
    async with async_session() as db:
        await db.execute(
            update(TaskRun)
            .where(TaskRun.task_id == task_id)
            .where(TaskRun.finished_at.is_(None))
            .values(status="failed", finished_at=datetime.now(UTC), error_log=reason)
        )
        await db.commit()


async def add_daily_stat(
    *,
    completed: int = 0,
    failed: int = 0,
    cost_usd: Decimal = Decimal("0"),
    duration_ms: int = 0,
    turns: int = 0,
) -> None:
    today: date = datetime.now(UTC).date()
    async with async_session() as db:
        stat = await db.get(DailyStat, today)
        if stat is None:
            stat = DailyStat(
                date=today, completed=0, failed=0, cost_usd=0, total_duration_ms=0, total_turns=0
            )
            db.add(stat)
        stat.completed += completed
        stat.failed += failed
        stat.cost_usd += cost_usd
        stat.total_duration_ms += duration_ms
        stat.total_turns += turns
        await db.commit()
