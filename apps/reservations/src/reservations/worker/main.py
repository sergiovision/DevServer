"""Worker process: ``python -m reservations.worker.main``.

Runs the PgQueuer consumer for ``reservations-chunk`` and
``reservations-webhook``. Any number of these processes may run at once.

Crash safety has two layers:

1. PgQueuer ``retry_timer`` (= ``STUCK_ITEM_TIMEOUT_S``): a job whose worker
   stopped heartbeating is picked up again by another worker.
2. A periodic sweep re-enqueues items stuck in ``processing`` longer than
   ``STUCK_ITEM_TIMEOUT_S`` (covers jobs that ended in an exception, e.g. the
   database vanished mid-chunk). Duplicate runs are harmless: every unit
   re-checks the item status under ``FOR UPDATE``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import asyncpg
import httpx
from pgqueuer import PgQueuer
from pgqueuer.models import Job
from pgqueuer.queries import Queries

from reservations.chunking import ChunkItem, plan_chunks
from reservations.config import Settings, get_settings
from reservations.db import close_pool, create_pool
from reservations.logs import setup_logging
from reservations.queue import CHUNK_ENTRYPOINT, WEBHOOK_ENTRYPOINT, enqueue_chunks
from reservations.worker.process_chunk import WorkerDeps, process_chunk_job
from reservations.worker.webhooks import deliver_webhook_job

logger = logging.getLogger(__name__)


def build_pgqueuer(deps: WorkerDeps) -> PgQueuer:
    settings = deps.settings
    pgq = PgQueuer.from_asyncpg_pool(deps.pool)
    rps = settings.worker_requests_per_second or float("inf")

    @pgq.entrypoint(
        CHUNK_ENTRYPOINT,
        concurrency_limit=settings.worker_concurrency,
        requests_per_second=rps,
        retry_timer=settings.job_retry_timer,
    )
    async def reservations_chunk(job: Job) -> None:
        await process_chunk_job(job, deps)

    @pgq.entrypoint(
        WEBHOOK_ENTRYPOINT,
        concurrency_limit=settings.worker_concurrency,
        retry_timer=settings.job_retry_timer,
    )
    async def reservations_webhook(job: Job) -> None:
        await deliver_webhook_job(job, deps)

    return pgq


async def requeue_stuck_items(pool: asyncpg.Pool, settings: Settings) -> int:
    """Re-enqueue chunks for items stuck in ``processing``. Returns item count."""
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """
            WITH stuck AS (
                SELECT id FROM reservation_batch_items
                 WHERE status = 'processing'
                   AND updated_at < now() - make_interval(secs => $1)
                 ORDER BY batch_id, seq
                 LIMIT 5000
                   FOR UPDATE SKIP LOCKED
            )
            UPDATE reservation_batch_items i SET updated_at = now()
              FROM stuck WHERE i.id = stuck.id
            RETURNING i.id, i.batch_id, i.client_id, i.seq, i.group_id
            """,
            settings.stuck_item_timeout_s,
        )
        by_batch: dict[tuple[UUID, str], list[ChunkItem]] = {}
        for r in rows:
            by_batch.setdefault((r["batch_id"], r["client_id"]), []).append(
                ChunkItem(r["id"], r["seq"], r["group_id"])
            )
        for (batch_id, client_id), items in by_batch.items():
            # Partial groups are fine: the chunk job always loads whole groups.
            await enqueue_chunks(conn, batch_id, client_id, plan_chunks(items, settings.chunk_size))
    if rows:
        logger.warning("re-enqueued stuck items", extra={"items": len(rows)})
    return len(rows)


async def _sweep_loop(pool: asyncpg.Pool, settings: Settings) -> None:
    interval = max(5.0, settings.stuck_item_timeout_s / 2)
    while True:
        try:
            await requeue_stuck_items(pool, settings)
        except Exception:
            logger.exception("stuck-item sweep failed")
        await asyncio.sleep(interval)


@dataclass
class WorkerHandle:
    deps: WorkerDeps
    pgq: PgQueuer
    runner: asyncio.Task[None]
    sweeper: asyncio.Task[None]

    async def stop(self, timeout: float = 15) -> None:
        self.pgq.shutdown.set()
        self.sweeper.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.runner), timeout)
        except TimeoutError:
            self.runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.runner
        with contextlib.suppress(asyncio.CancelledError):
            await self.sweeper
        await self.deps.http.aclose()
        await close_pool(self.deps.pool)


async def start_worker(settings: Settings) -> WorkerHandle:
    # Each running chunk holds two connections (slot lock + work), plus PgQueuer's own.
    pool = await create_pool(settings, max_size=settings.worker_concurrency * 2 + 8)
    deps = WorkerDeps(
        pool=pool,
        settings=settings,
        queries=Queries.from_asyncpg_pool(pool),
        http=httpx.AsyncClient(timeout=settings.webhook_timeout_s),
    )
    pgq = build_pgqueuer(deps)
    # Short dequeue timeout: deferred jobs (client busy, webhook backoff) become
    # eligible without a NOTIFY, so idle workers must poll often enough.
    runner = asyncio.create_task(pgq.run(dequeue_timeout=timedelta(seconds=1)), name="pgqueuer")
    sweeper = asyncio.create_task(_sweep_loop(pool, settings), name="stuck-sweep")
    logger.info(
        "worker started",
        extra={
            "concurrency": settings.worker_concurrency,
            "client_max_concurrent_chunks": settings.client_max_concurrent_chunks,
        },
    )
    return WorkerHandle(deps=deps, pgq=pgq, runner=runner, sweeper=sweeper)


async def _main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    handle = await start_worker(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle.pgq.shutdown.set)
    await handle.pgq.shutdown.wait()
    logger.info("worker shutting down")
    await handle.stop()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
