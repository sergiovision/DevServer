"""Night Cycle — runs the task backlog continuously until morning.

Enqueues tasks one by one, retries failed tasks (with a worker restart for
a clean slate), and loops until the configured end hour is reached.

State is persisted to PostgreSQL (worker_state table) so the cycle survives
worker restarts.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, text

from config import settings
from models.base import async_session
from models.task import Task

logger = logging.getLogger(__name__)

_STATE_KEY = "night-cycle"
_WEB_PORT = 3000  # Next.js port

_task: asyncio.Task | None = None


# ─── PostgreSQL state helpers ───────────────────────────────────────────────

async def _save(state: dict) -> None:
    """Persist night cycle state to worker_state table."""
    state["log"] = state.get("log", [])[-100:]  # cap at 100 lines
    async with async_session() as db:
        await db.execute(text("""
            INSERT INTO worker_state (key, value, updated_at)
            VALUES (:key, :value, NOW())
            ON CONFLICT (key) DO UPDATE SET value = :value, updated_at = NOW()
        """), {"key": _STATE_KEY, "value": json.dumps(state)})
        await db.commit()


async def _load() -> dict | None:
    """Load night cycle state from worker_state table."""
    async with async_session() as db:
        result = await db.execute(text(
            "SELECT value FROM worker_state WHERE key = :key"
        ), {"key": _STATE_KEY})
        row = result.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except Exception:
            return None


async def _delete_state() -> None:
    """Delete night cycle state from worker_state table."""
    async with async_session() as db:
        await db.execute(text(
            "DELETE FROM worker_state WHERE key = :key"
        ), {"key": _STATE_KEY})
        await db.commit()


# ─── DB helpers ──────────────────────────────────────────────────────────────

async def _get_workload(state: dict) -> list[dict]:
    """Return pending tasks + previously failed tasks to retry."""
    async with async_session() as db:
        res = await db.execute(
            select(Task).where(Task.status == "pending").order_by(Task.priority, Task.id)
        )
        pending = res.scalars().all()

        failed_ids = state.get("failed_task_ids", [])
        failed = []
        if failed_ids:
            res2 = await db.execute(
                select(Task)
                .where(Task.id.in_(failed_ids), Task.status == "failed")
                .order_by(Task.priority, Task.id)
            )
            failed = list(res2.scalars().all())

    seen: set[int] = set()
    tasks: list[dict] = []
    for t in list(pending) + failed:
        if t.id not in seen:
            tasks.append({
                "id": t.id,
                "task_key": t.task_key,
                "title": t.title,
                "claude_mode": t.claude_mode or "max",
                "status": t.status,
            })
            seen.add(t.id)
    return tasks


async def _get_task_status(task_id: int) -> str:
    async with async_session() as db:
        task = await db.get(Task, task_id)
        return task.status if task else "cancelled"


# ─── Actions ─────────────────────────────────────────────────────────────────

async def _enqueue(task_id: int) -> bool:
    """Call Next.js enqueue endpoint."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"http://localhost:{_WEB_PORT}/api/tasks/{task_id}/enqueue")
            return resp.status_code == 200
    except Exception:
        logger.exception("Failed to enqueue task %d", task_id)
        return False


async def _trigger_restart(state: dict, task_id: int) -> None:
    """Persist state then ask Next.js to restart this process.

    The process will be killed; on next startup lifespan calls resume_if_active()
    which continues the cycle from PostgreSQL state.
    """
    state["restarting_for_task_id"] = task_id
    await _save(state)
    logger.info("Night cycle: requesting worker restart for task %d", task_id)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"http://localhost:{_WEB_PORT}/api/worker",
                json={"action": "restart"},
            )
    except Exception:
        pass  # Expected — pkill kills us mid-request
    # Wait to be killed; if still alive after 15 s, continue without restart
    await asyncio.sleep(15)


# ─── Core loop ───────────────────────────────────────────────────────────────

async def _run(state: dict) -> None:
    try:
        # Handle resume after a worker restart triggered by night cycle
        resumed_for = state.pop("restarting_for_task_id", None)
        if resumed_for is not None:
            state.setdefault("restarted_for_ids", [])
            if resumed_for not in state["restarted_for_ids"]:
                state["restarted_for_ids"].append(resumed_for)
            ts = datetime.now(timezone.utc).strftime("%H:%M")
            state["log"].append(f"[{ts}] Worker restarted, resuming")
            await _save(state)

        while True:
            now = datetime.now(timezone.utc)
            ts = now.strftime("%H:%M")
            end_time = datetime.fromisoformat(state["end_time"])

            if now >= end_time:
                logger.info("Night cycle: morning reached, stopping")
                state["active"] = False
                state["log"].append(f"[{ts}] Morning \u2014 night cycle complete")
                await _save(state)
                await _delete_state()
                return

            workload = await _get_workload(state)
            if not workload:
                state["log"].append(f"[{ts}] No tasks \u2014 sleeping 60 s")
                await _save(state)
                await asyncio.sleep(60)
                continue

            state["cycle_count"] = state.get("cycle_count", 0) + 1
            state["log"].append(
                f"[{ts}] Cycle {state['cycle_count']}: {len(workload)} task(s)"
            )
            await _save(state)

            for task in workload:
                task_id = task["id"]

                if datetime.now(timezone.utc) >= end_time:
                    break

                # Restart worker before retrying a previously failed task (once per task)
                is_retry = task["status"] == "failed"
                already_restarted = task_id in state.get("restarted_for_ids", [])

                if is_retry and not already_restarted:
                    ts = datetime.now(timezone.utc).strftime("%H:%M")
                    state["log"].append(
                        f"[{ts}] Restarting worker before retry: {task['task_key']}"
                    )
                    await _trigger_restart(state, task_id)
                    # If still alive, mark as restarted and continue
                    state.setdefault("restarted_for_ids", []).append(task_id)
                    await _save(state)

                ts = datetime.now(timezone.utc).strftime("%H:%M")
                state["current_task_id"] = task_id
                state["log"].append(f"[{ts}] \u2192 {task['task_key']}")
                await _save(state)

                enqueued = await _enqueue(task_id)
                if not enqueued:
                    state["log"].append(f"  Failed to enqueue {task['task_key']}")
                    state["current_task_id"] = None
                    await _save(state)
                    continue

                # Poll for completion (up to 3 h)
                for _ in range(360):
                    await asyncio.sleep(30)
                    status = await _get_task_status(task_id)
                    if status in ("test", "failed", "cancelled"):
                        break

                final_status = await _get_task_status(task_id)
                state["current_task_id"] = None

                if final_status == "test":
                    state.setdefault("completed_task_ids", []).append(task_id)
                    state["failed_task_ids"] = [
                        t for t in state.get("failed_task_ids", []) if t != task_id
                    ]
                    state["restarted_for_ids"] = [
                        t for t in state.get("restarted_for_ids", []) if t != task_id
                    ]
                    state["log"].append(f"  \u2713 done")
                else:
                    if task_id not in state.get("failed_task_ids", []):
                        state.setdefault("failed_task_ids", []).append(task_id)
                    # Clear restarted flag so next cycle restarts again
                    state["restarted_for_ids"] = [
                        t for t in state.get("restarted_for_ids", []) if t != task_id
                    ]
                    state["log"].append(f"  \u2717 {final_status}")

                await _save(state)

            await asyncio.sleep(15)  # brief pause between cycles

    except asyncio.CancelledError:
        logger.info("Night cycle cancelled")
        raise


# ─── Public API ──────────────────────────────────────────────────────────────

def is_running() -> bool:
    return _task is not None and not _task.done()


async def get_status() -> dict | None:
    state = await _load()
    if state is None:
        return None
    return {**state, "task_running": is_running()}


async def start(end_hour: int = 7) -> dict:
    global _task
    if is_running():
        return {"error": "Night cycle already running"}

    now = datetime.now(timezone.utc)
    end_time = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if end_time <= now:
        end_time += timedelta(days=1)

    state: dict = {
        "active": True,
        "started_at": now.isoformat(),
        "end_time": end_time.isoformat(),
        "end_hour": end_hour,
        "cycle_count": 0,
        "current_task_id": None,
        "completed_task_ids": [],
        "failed_task_ids": [],
        "restarted_for_ids": [],
        "restart_on_failed": True,
        "log": [
            f"[{now.strftime('%H:%M')}] Night cycle started \u2014 runs until {end_time.strftime('%H:%M')} UTC"
        ],
    }

    await _save(state)
    _task = asyncio.create_task(_run(state))
    logger.info("Night cycle started, ends at %s UTC", end_time.strftime("%H:%M"))
    return {"started": True, "end_time": end_time.isoformat()}


async def stop() -> dict:
    global _task
    await _delete_state()

    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None

    return {"stopped": True}


async def resume_if_active() -> None:
    """Called from lifespan — resumes night cycle if PostgreSQL state says it was active."""
    global _task
    state = await _load()
    if not state or not state.get("active"):
        return

    end_time = datetime.fromisoformat(state["end_time"])
    if datetime.now(timezone.utc) >= end_time:
        logger.info("Night cycle: past end time on resume, clearing")
        await _delete_state()
        return

    logger.info(
        "Night cycle: resuming (cycle=%d, completed=%d, failed=%d)",
        state.get("cycle_count", 0),
        len(state.get("completed_task_ids", [])),
        len(state.get("failed_task_ids", [])),
    )
    _task = asyncio.create_task(_run(state))
