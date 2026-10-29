"""PgQueuer consumer: picks jobs from the ``devserver-tasks`` queue and runs them.

Each job runs ``run_task`` in its own asyncio task, registered with
:data:`services.running_tasks.registry` so an operator's Cancel can stop it.
A job whose repository is busy is not failed: it is deferred with a delay and
picked up again once the other task releases the repo lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import asyncpg
from pgqueuer import PgQueuer
from pgqueuer.models import Job
from pgqueuer.queries import Queries

from config import settings
from services.agent_runner import run_task
from services.repo_lock import RepoLockBusy
from services.runner.store import set_task_columns
from services.running_tasks import registry

logger = logging.getLogger(__name__)

ENTRYPOINT = "devserver-tasks"
#: How long a job waits before retrying a busy repository.
LOCK_RETRY_DELAY = timedelta(seconds=60)

_pgq: PgQueuer | None = None
_pool: asyncpg.Pool | None = None
_runner_task: asyncio.Task | None = None


class JobFailedError(RuntimeError):
    """Raised so PgQueuer records the job as failed."""


@dataclass(frozen=True)
class JobRequest:
    task_id: int
    billing_mode: str
    max_turns: int | None


def parse_job(job: Job) -> JobRequest | None:
    """Decode a job payload; None (logged) when it is unusable."""
    if not job.payload:
        logger.error("Job %s has no payload", job.id)
        return None
    try:
        data = json.loads(job.payload.decode())
    except json.JSONDecodeError, UnicodeDecodeError:
        logger.error("Job %s has invalid payload: %s", job.id, job.payload)
        return None
    if not data.get("taskId"):
        logger.error("Job %s has no taskId in payload: %s", job.id, data)
        return None
    return JobRequest(
        task_id=int(data["taskId"]),
        billing_mode=data.get("claudeMode", "max"),
        max_turns=data.get("maxTurns"),
    )


async def _requeue_later(queries: Queries, job: Job, request: JobRequest) -> None:
    """Re-enqueue the same payload with a delay; keep the task pointing at it."""
    (new_job_id,) = await queries.enqueue(
        ENTRYPOINT, job.payload, priority=job.priority, execute_after=LOCK_RETRY_DELAY
    )
    await set_task_columns(request.task_id, queue_job_id=str(new_job_id))
    logger.info("Task %s: repo busy, re-queued as job %s", request.task_id, new_job_id)


async def execute_job(job: Job, queries: Queries) -> None:
    """Run one job to completion. Raises :class:`JobFailedError` on failure."""
    request = parse_job(job)
    if request is None:
        return
    logger.info(
        "Processing job %s for task_id=%s (claude_mode=%s, max_turns=%s)",
        job.id,
        request.task_id,
        request.billing_mode,
        request.max_turns,
    )
    run = asyncio.create_task(
        run_task(request.task_id, claude_mode=request.billing_mode, max_turns=request.max_turns),
        name=f"task:{request.task_id}",
    )
    with registry.track(request.task_id, run):
        try:
            success = await run
        except RepoLockBusy:
            await _requeue_later(queries, job, request)
            return
        except asyncio.CancelledError:
            if not registry.was_cancel_requested(request.task_id):
                raise  # the worker is shutting down
            logger.info("Job %s (task %s) cancelled by operator", job.id, request.task_id)
            return
    if not success:
        raise JobFailedError(f"Task {request.task_id} execution failed")
    logger.info("Job %s (task %s) completed successfully", job.id, request.task_id)


async def _create_pgqueuer() -> PgQueuer:
    global _pool
    # asyncpg wants the plain postgresql:// URL, not SQLAlchemy's +asyncpg form.
    db_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    _pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    pgq = PgQueuer.from_asyncpg_pool(_pool)
    queries = Queries.from_asyncpg_pool(_pool)

    @pgq.entrypoint(ENTRYPOINT, concurrency_limit=settings.worker_concurrency)
    async def process_task(job: Job) -> None:
        await execute_job(job, queries)

    return pgq


async def start_consumer() -> None:
    """Start the PgQueuer consumer in the background."""
    global _pgq, _runner_task
    logger.info(
        "Starting PgQueuer consumer (concurrency=%d, db=%s)",
        settings.worker_concurrency,
        settings.database_url.split("@")[-1] if "@" in settings.database_url else "local",
    )
    _pgq = await _create_pgqueuer()
    _runner_task = asyncio.create_task(_pgq.run())
    logger.info("PgQueuer consumer started")


async def stop_consumer() -> None:
    """Stop the consumer and close its connection pool."""
    global _pgq, _pool, _runner_task
    if _runner_task and not _runner_task.done():
        logger.info("Stopping PgQueuer consumer...")
        _runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _runner_task
    _runner_task = None
    if _pool is not None:
        await _pool.close()
        _pool = None
    _pgq = None
    logger.info("PgQueuer consumer stopped")


def get_consumer() -> PgQueuer | None:
    """Return the current PgQueuer instance (for status checks)."""
    return _pgq


def is_consumer_running() -> bool:
    """Check if the consumer task is alive."""
    return _runner_task is not None and not _runner_task.done()
