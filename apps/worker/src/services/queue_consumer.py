"""PgQueuer consumer — picks jobs from the 'devserver-tasks' queue and runs them."""

import asyncio
import json
import logging

import asyncpg
from pgqueuer import PgQueuer
from pgqueuer.models import Job

from config import settings
from services.agent_runner import run_task

logger = logging.getLogger(__name__)

_pgq: PgQueuer | None = None
_runner_task: asyncio.Task | None = None


async def _create_pgqueuer() -> PgQueuer:
    """Create and configure a PgQueuer instance."""
    # asyncpg needs plain postgresql:// URL (not the SQLAlchemy postgresql+asyncpg:// variant)
    db_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    pgq = PgQueuer.from_asyncpg_pool(pool)

    @pgq.entrypoint(
        "devserver-tasks",
        concurrency_limit=settings.worker_concurrency,
    )
    async def process_task(job: Job) -> None:
        """Process a single queued task."""
        if not job.payload:
            logger.error("Job %s has no payload", job.id)
            return

        try:
            data = json.loads(job.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.error("Job %s has invalid payload: %s", job.id, job.payload)
            return

        task_id = data.get("taskId")
        if not task_id:
            logger.error("Job %s has no taskId in payload: %s", job.id, data)
            return

        claude_mode = data.get("claudeMode", "max")
        max_turns = data.get("maxTurns")
        logger.info(
            "Processing job %s for task_id=%s (claude_mode=%s, max_turns=%s)",
            job.id, task_id, claude_mode, max_turns,
        )

        success = await run_task(int(task_id), claude_mode=claude_mode, max_turns=max_turns)
        if success:
            logger.info("Job %s (task %s) completed successfully", job.id, task_id)
        else:
            logger.warning("Job %s (task %s) failed", job.id, task_id)
            raise Exception(f"Task {task_id} execution failed")

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
    """Gracefully stop the PgQueuer consumer."""
    global _pgq, _runner_task

    if _runner_task and not _runner_task.done():
        logger.info("Stopping PgQueuer consumer...")
        _runner_task.cancel()
        try:
            await _runner_task
        except asyncio.CancelledError:
            pass
        _runner_task = None

    _pgq = None
    logger.info("PgQueuer consumer stopped")


def get_consumer() -> PgQueuer | None:
    """Return the current PgQueuer instance (for status checks)."""
    return _pgq


def is_consumer_running() -> bool:
    """Check if the consumer task is alive."""
    return _runner_task is not None and not _runner_task.done()
