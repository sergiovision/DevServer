"""Operator actions on an existing task: approve, reject, retry, cancel,
continue and compact.

Cancellation goes through ``services.task_control`` so a running agent is
really stopped, not just relabelled.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update

from models.base import async_session
from models.task import Task
from routes.tasks import ContinueTaskRequest
from services import (
    compaction,
    task_control,
)

router = APIRouter(prefix="/internal")


# ─── Task commands ───────────────────────────────────────────────────────────


@router.post("/tasks/{task_key}/approve")
async def approve_task(task_key: str):
    """Approve a pending task — set status to queued."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")
        if task.status not in ("pending", "blocked"):
            raise HTTPException(status_code=400, detail=f"Task is {task.status}, cannot approve")

        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="queued", updated_at=datetime.now(UTC))
        )
        await db.commit()

    return {"task_key": task_key, "status": "queued", "message": f"\u2705 Approved {task_key}"}


@router.post("/tasks/{task_key}/reject")
async def reject_task(task_key: str):
    """Reject a pending task."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")

        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="cancelled", updated_at=datetime.now(UTC))
        )
        await db.commit()

    return {
        "task_key": task_key,
        "status": "cancelled",
        "message": f"\U0001f6ab Rejected {task_key}",
    }


@router.post("/tasks/{task_key}/retry")
async def retry_task(task_key: str):
    """Re-queue a failed task."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")
        if task.status not in ("failed", "cancelled"):
            raise HTTPException(
                status_code=400, detail=f"Task is {task.status}, can only retry failed/cancelled"
            )

        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="queued", updated_at=datetime.now(UTC))
        )
        await db.commit()

    return {"task_key": task_key, "status": "queued", "message": f"\U0001f504 Re-queued {task_key}"}


async def _cancel(*, task_id: int | None = None, task_key: str | None = None) -> dict:
    try:
        cancelled = await task_control.cancel(task_id=task_id, task_key=task_key)
    except task_control.TaskNotFoundError:
        raise HTTPException(404, "Task not found") from None
    except task_control.TaskStateError as exc:
        raise HTTPException(400, str(exc)) from None
    return {
        "task_id": cancelled.task_id,
        "task_key": cancelled.task_key,
        "old_status": cancelled.old_status,
        "new_status": "cancelled",
        "agent_stopped": cancelled.stopped_running_agent,
    }


@router.post("/tasks/{task_key}/cancel")
async def cancel_task_by_key(task_key: str):
    """Cancel a task by key, stopping its agent if one is running."""
    return await _cancel(task_key=task_key)


@router.post("/cancel/{task_id}")
async def cancel_task(task_id: int):
    """Cancel a task by DB id, stopping its agent if one is running."""
    return await _cancel(task_id=task_id)


# ─── Task continuation ──────────────────────────────────────────────────────


@router.post("/tasks/{task_key}/continue")
async def continue_task(task_key: str, req: ContinueTaskRequest):
    """Prepare a task for continuation with an optional model/mode switch.

    If the task is currently running, in-flight runs are marked failed
    (same as cancel) but git state and session are preserved.  The repo
    lock is released so the re-enqueued job can acquire it immediately.
    The caller (web API) is expected to re-enqueue the task after this
    returns.
    """
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")
        # ``test`` is a post-verify "waiting for human QA" state that still
        # has a live agent branch + session_id, so operators can drop a
        # follow-up message into the inbox and continue the same task
        # without reopening (which would reset the session).
        if task.status in ("done", "retired"):
            raise HTTPException(
                status_code=400,
                detail=f"Task is {task.status}, cannot continue",
            )

        old_status = task.status

        # A running agent is stopped first (its session id is kept, so the
        # continuation resumes it) and only this task's repo lock is released.
        if old_status in ("running", "verifying"):
            await task_control.interrupt_for_continuation(task.id, task.task_key)
            await db.refresh(task)

        # Apply optional model/mode overrides.
        updates: dict = {
            "is_continuation": True,
            "status": "pending",
            "updated_at": datetime.now(UTC),
        }
        if req.model is not None:
            updates["claude_model"] = req.model or None
        if req.mode is not None and req.mode in ("max", "api"):
            updates["claude_mode"] = req.mode

        await db.execute(update(Task).where(Task.id == task.id).values(**updates))
        await db.commit()

    return {
        "task_key": task_key,
        "old_status": old_status,
        "new_status": "pending",
        "model": req.model,
        "mode": req.mode,
    }


# ─── Context compaction ────────────────────────────────────────────────────


@router.post("/tasks/{task_key}/compact")
async def compact_task(task_key: str, reason: str = "manual"):
    """Summarise a task's transcript via the system LLM.

    Writes the result onto ``tasks.compacted_context`` and emits a
    ``context_compacted`` event. The next attempt will inject the
    summary as its sole context block (repo map/memory/reality signal
    are skipped).

    The caller is responsible for separately clearing ``session_id``
    or triggering a continuation. Invoking ``/tasks/<key>/continue``
    after ``/compact`` is the standard recovery path from the dashboard.
    """
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(404, f"Task {task_key} not found")
        result = await compaction.compact_task(
            db,
            task_id=task.id,
            reason=reason,
        )
    if not result["ok"]:
        raise HTTPException(502, result.get("error") or "compaction failed")
    return {
        "task_key": task_key,
        "chars_in": result["chars_in"],
        "chars_out": result["chars_out"],
        "compression_ratio": (
            round(result["chars_out"] / result["chars_in"], 3) if result["chars_in"] else None
        ),
    }
