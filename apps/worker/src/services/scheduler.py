"""Background scheduler with a named-job registry.

Each registered job runs on its own schedule (interval or daily) and exposes
metadata for the /internal/jobs API: name, group, schedule description,
is_running, prev_time, next_time, and the last log message.

Run-now is implemented by advancing next_time to `now`; Stop-now cancels the
currently executing handler task (the outer loop keeps running).
"""

import asyncio
import json as _json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from sqlalchemy import func, select, text

from config import settings
from models.base import async_session
from models.daily_stat import DailyStat
from models.task import Task
from services.telegram import tg_send

# Pro features: rich daily digest replaces the basic one when available.
try:
    from services.pro import hooks as pro
    _has_pro = True
except ImportError:
    _has_pro = False

logger = logging.getLogger(__name__)


# ─── Job registry ───────────────────────────────────────────────────────────

@dataclass
class Job:
    name: str
    group: str
    schedule: str
    next_time: float  # epoch seconds
    interval_seconds: Optional[int] = None
    daily_hour_utc: Optional[int] = None
    prev_time: Optional[float] = None
    is_running: bool = False
    log: str = ""
    handler: Optional[Callable[[], Awaitable[str]]] = None
    loop_task: Optional[asyncio.Task] = None
    handler_task: Optional[asyncio.Task] = None


_JOBS: dict[str, Job] = {}


def _compute_next_daily(hour: int) -> float:
    now = datetime.now(timezone.utc)
    next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= next_run:
        next_run += timedelta(days=1)
    return next_run.timestamp()


async def _job_loop(job: Job):
    """Outer loop: wait until next_time, then invoke handler as a child task."""
    while True:
        try:
            # Sleep in small slices so run-now wakes us within a few seconds.
            while True:
                remaining = job.next_time - time.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 5.0))

            job.is_running = True
            job.prev_time = time.time()
            handler_task = asyncio.create_task(job.handler())  # type: ignore[misc]
            job.handler_task = handler_task
            try:
                result = await handler_task
                job.log = result or "OK"
            except asyncio.CancelledError:
                # Stop-now cancelled the handler; loop continues.
                job.log = "Stopped"
            except Exception as exc:
                logger.exception("Job %s failed", job.name)
                job.log = f"Error: {exc}"
            finally:
                job.is_running = False
                job.handler_task = None

            if job.interval_seconds is not None:
                job.next_time = time.time() + job.interval_seconds
            elif job.daily_hour_utc is not None:
                job.next_time = _compute_next_daily(job.daily_hour_utc)
            else:
                return
        except asyncio.CancelledError:
            logger.info("Job loop %s cancelled", job.name)
            raise
        except Exception:
            logger.exception("Job loop %s crashed — restarting in 60s", job.name)
            await asyncio.sleep(60)


def _register(job: Job, handler: Callable[[], Awaitable[str]]):
    job.handler = handler
    _JOBS[job.name] = job


# ─── Job handlers (single-run) ──────────────────────────────────────────────

async def _run_stale_task_recovery() -> str:
    recovered: list[str] = []
    async with async_session() as session:
        result = await session.execute(text("""
            SELECT t.id, t.task_key, t.queue_job_id, r.name AS repo_name
            FROM tasks t
            JOIN repos r ON t.repo_id = r.id
            WHERE t.status IN ('running', 'verifying')
              AND t.updated_at < NOW() - (r.timeout_minutes || ' minutes')::interval
        """))
        stale_rows = result.mappings().all()
        for row in stale_rows:
            await session.execute(text("""
                UPDATE tasks SET status = 'pending', queue_job_id = NULL, updated_at = NOW()
                WHERE id = :id
            """), {"id": row["id"]})
            await session.execute(text("""
                DELETE FROM repo_locks WHERE repo_name = :repo_name
            """), {"repo_name": row["repo_name"]})
            recovered.append(f"{row['task_key']} ({row['repo_name']})")
            logger.info("Recovered stale task: %s", row["task_key"])
        await session.commit()

    if recovered:
        message = (
            "\U0001f504 *Stale Task Recovery*\n\n"
            f"Recovered {len(recovered)} stale tasks:\n"
            + "\n".join(f"\u2022 {task}" for task in recovered)
        )
        await tg_send(message)
        return f"Recovered {len(recovered)} stale tasks"
    return "No stale tasks"


async def _run_daily_report() -> str:
    # Pro daily digest: richer formatting with 7-day trends and sparklines
    if _has_pro:
        async with async_session() as session:
            result = await pro.tg_send_daily_digest(db=session)
        if result:
            deleted_files = await _cleanup_old_logs()
            return f"{result} | cleaned {deleted_files} old logs"

    # Free-tier daily report — basic stats
    yesterday = (datetime.utcnow() - timedelta(days=1)).date()

    async with async_session() as session:
        result = await session.execute(
            select(DailyStat).where(DailyStat.date == yesterday)
        )
        daily_stat = result.scalar_one_or_none()

        pending_result = await session.execute(
            select(func.count(Task.id)).where(Task.status == "pending")
        )
        pending_count = pending_result.scalar()

    if daily_stat:
        completed = daily_stat.completed
        failed = daily_stat.failed
        cost = daily_stat.cost_usd
        turns = daily_stat.total_turns
        duration_ms = daily_stat.total_duration_ms
        avg_duration = duration_ms / max(completed + failed, 1) / 1000 / 60
    else:
        completed = failed = turns = 0
        cost = Decimal("0.0000")
        avg_duration = 0

    deleted_files = await _cleanup_old_logs()

    message = (
        f"\U0001f4ca *Daily Report* - {yesterday.strftime('%Y-%m-%d')}\n\n"
        f"\u2705 Completed: {completed}\n"
        f"\u274c Failed: {failed}\n"
        f"\U0001f4b0 Cost: ${cost:.4f}\n"
        f"\U0001f504 Turns: {turns}\n"
        f"\u23f1\ufe0f Avg Duration: {avg_duration:.1f}m\n\n"
        f"\U0001f4cb Pending Backlog: {pending_count}\n"
        f"\U0001f9f9 Cleaned {deleted_files} old log files"
    )
    await tg_send(message)
    return f"Sent report ({completed} done, {failed} failed)"


async def _run_memory_archive() -> str:
    """Archive never-recalled stale agent memories (Pro, migration 010).

    Opt-in via the ``memory_archive_days`` setting — 0 (default) is a no-op,
    so this job is cheap and harmless until an operator turns it on.
    """
    if not _has_pro:
        return "Pro not installed"
    async with async_session() as session:
        archived = await pro.archive_stale_memories(session=session)
    return f"Archived {archived} stale memories" if archived else "No stale memories"


async def _cleanup_old_logs() -> int:
    if not os.path.exists(settings.log_dir):
        return 0
    cutoff = datetime.utcnow() - timedelta(days=7)
    deleted_count = 0
    try:
        for filename in os.listdir(settings.log_dir):
            file_path = os.path.join(settings.log_dir, filename)
            if os.path.isfile(file_path):
                file_mtime = datetime.utcfromtimestamp(os.path.getmtime(file_path))
                if file_mtime < cutoff:
                    os.remove(file_path)
                    deleted_count += 1
    except Exception:
        logger.exception("Log cleanup failed")
    return deleted_count


# ─── DB-backed schedules (Phase 1) ───────────────────────────────────────────

_SCHEDULE_GROUP = "schedule"


def _parse_cron(expr: str) -> tuple[Optional[int], Optional[int], str]:
    """Map a small cron subset → (interval_seconds, daily_hour_utc, label).

    Supported: '@hourly', '@daily'/'@midnight', 'every <N>m', 'every <N>h',
    'HH:MM' (daily UTC). Anything else falls back to hourly.
    """
    e = (expr or "").strip().lower()
    if e == "@hourly":
        return 3600, None, "every hour"
    if e in ("@daily", "@midnight"):
        return None, 0, "daily 00:00 UTC"
    m = re.match(r"^every\s+(\d+)\s*m(?:in)?$", e)
    if m:
        return int(m.group(1)) * 60, None, f"every {m.group(1)}m"
    m = re.match(r"^every\s+(\d+)\s*h(?:our)?s?$", e)
    if m:
        return int(m.group(1)) * 3600, None, f"every {m.group(1)}h"
    m = re.match(r"^(\d{1,2}):(\d{2})$", e)
    if m:
        return None, int(m.group(1)), f"daily {expr.strip()} UTC"
    return 3600, None, f"{expr} (unrecognised → hourly)"


async def _fire_schedule(schedule_id: int) -> str:
    """Re-run the schedule's bound task and bump bookkeeping.

    A schedule references one existing task (schedules.task_id). Firing it
    resets that task to 'pending' and enqueues it through the Next.js
    enqueue endpoint (the single source of truth for the queue). A task
    that is already in flight (queued/running/verifying) is left alone —
    the fire is skipped, only the next-run bookkeeping advances.
    """
    async with async_session() as db:
        row = (await db.execute(text(
            """
            SELECT s.id, s.name, s.cron_expr, s.task_id,
                   t.task_key, t.status AS task_status
            FROM schedules s
            LEFT JOIN tasks t ON t.id = s.task_id
            WHERE s.id = :id AND s.enabled
            """
        ), {"id": schedule_id})).mappings().fetchone()
        if not row:
            return f"schedule {schedule_id} gone or disabled"

        now = datetime.now(timezone.utc)
        interval, daily_hour, _label = _parse_cron(row["cron_expr"])
        if interval is not None:
            next_run = now + timedelta(seconds=interval)
        else:
            next_run = datetime.fromtimestamp(_compute_next_daily(daily_hour or 0), tz=timezone.utc)
        await db.execute(text(
            "UPDATE schedules SET last_run_at = :now, next_run_at = :nx, updated_at = :now WHERE id = :id"
        ), {"now": now, "nx": next_run, "id": schedule_id})

        task_id = row["task_id"]
        task_key = row["task_key"]
        if not task_id or not task_key:
            await db.commit()
            return f"schedule {schedule_id} has no task bound — skipped"

        if row["task_status"] in ("queued", "running", "verifying"):
            await db.commit()
            return (f"schedule {schedule_id}: task {task_key} is "
                    f"{row['task_status']} — skipped this fire")

        await db.execute(text(
            "UPDATE tasks SET status = 'pending', updated_at = NOW() WHERE id = :t"
        ), {"t": task_id})
        await db.execute(text(
            "INSERT INTO task_events (task_id, event_type, payload) "
            "VALUES (:t, 'schedule_fired', CAST(:pl AS JSONB))"
        ), {"t": task_id, "pl": _json.dumps({
            "schedule_id": schedule_id, "name": row["name"],
            "task_key": task_key,
        })})
        await db.commit()

    enqueued = False
    try:
        from services.decomposer import _enqueue_task
        enqueued = await _enqueue_task(task_id)
    except Exception:
        logger.exception("schedule %s enqueue failed", schedule_id)
    return f"fired schedule {schedule_id} → task {task_key} (enqueued={enqueued})"


def _make_schedule_handler(schedule_id: int) -> Callable[[], Awaitable[str]]:
    async def _handler() -> str:
        return await _fire_schedule(schedule_id)
    return _handler


async def _register_db_schedules(*, start_loops: bool = False) -> int:
    """Load enabled schedules from the DB and register each as a Job.

    When ``start_loops`` is True (runtime reload), also starts the outer loop
    task for each newly-registered schedule job.
    """
    async with async_session() as db:
        rows = (await db.execute(text(
            "SELECT id, name, cron_expr, next_run_at FROM schedules WHERE enabled ORDER BY id"
        ))).mappings().all()

    count = 0
    for row in rows:
        interval, daily_hour, label = _parse_cron(row["cron_expr"])
        if interval is not None:
            next_time = time.time() + interval
        else:
            next_time = _compute_next_daily(daily_hour or 0)
        job = Job(
            name=f"{_SCHEDULE_GROUP}:{row['id']}",
            group=_SCHEDULE_GROUP,
            schedule=f"{row['name']} — {label}",
            interval_seconds=interval,
            daily_hour_utc=daily_hour,
            next_time=next_time,
        )
        _register(job, _make_schedule_handler(row["id"]))
        if start_loops:
            job.loop_task = asyncio.create_task(_job_loop(job))
        count += 1
    return count


async def reload_schedules() -> int:
    """Re-sync schedule jobs after a CRUD change. Cancels existing schedule
    loops and re-registers from the DB. Returns the new schedule count."""
    for name, job in list(_JOBS.items()):
        if job.group == _SCHEDULE_GROUP:
            if job.loop_task and not job.loop_task.done():
                job.loop_task.cancel()
            _JOBS.pop(name, None)
    count = await _register_db_schedules(start_loops=True)
    logger.info("Reloaded %d DB schedules", count)
    return count


# ─── Public API ─────────────────────────────────────────────────────────────

async def start_scheduler() -> list[asyncio.Task]:
    logger.info("Starting background scheduler...")
    _JOBS.clear()

    _register(
        Job(
            name="stale_task_recovery",
            group="devserver",
            schedule="every 15 minutes",
            interval_seconds=15 * 60,
            next_time=time.time() + 15 * 60,
        ),
        _run_stale_task_recovery,
    )
    _register(
        Job(
            name="daily_report",
            group="devserver",
            schedule="daily 06:00 UTC",
            daily_hour_utc=6,
            next_time=_compute_next_daily(6),
        ),
        _run_daily_report,
    )
    if _has_pro:
        _register(
            Job(
                name="memory_archive",
                group="devserver",
                schedule="every 6 hours",
                interval_seconds=6 * 60 * 60,
                next_time=time.time() + 6 * 60 * 60,
            ),
            _run_memory_archive,
        )

    # DB-backed schedules (Phase 1). Registered here so the loop-creation
    # below starts their outer loops alongside the built-in jobs.
    try:
        n = await _register_db_schedules()
        if n:
            logger.info("Registered %d DB schedules", n)
    except Exception:
        logger.exception("failed to register DB schedules")

    loops: list[asyncio.Task] = []
    for job in _JOBS.values():
        job.loop_task = asyncio.create_task(_job_loop(job))
        loops.append(job.loop_task)

    logger.info("Scheduler started with %d background jobs", len(loops))
    return loops


async def stop_scheduler():
    logger.info("Stopping background scheduler...")
    for job in _JOBS.values():
        if job.loop_task and not job.loop_task.done():
            job.loop_task.cancel()
    tasks = [j.loop_task for j in _JOBS.values() if j.loop_task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _JOBS.clear()
    logger.info("Scheduler stopped")


def _iso_utc(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def get_all_jobs() -> list[dict]:
    return [
        {
            "name": j.name,
            "group": j.group,
            "schedule": j.schedule,
            "is_running": j.is_running,
            "prev_time": _iso_utc(j.prev_time),
            "next_time": _iso_utc(j.next_time),
            "log": j.log,
        }
        for j in _JOBS.values()
    ]


def run_job_now(name: str) -> bool:
    job = _JOBS.get(name)
    if not job:
        return False
    # Advance schedule to "now" — the loop wakes within its 5s slice.
    job.next_time = time.time()
    return True


def stop_job_now(name: str) -> bool:
    job = _JOBS.get(name)
    if not job:
        return False
    if job.handler_task and not job.handler_task.done():
        job.handler_task.cancel()
        return True
    return False
