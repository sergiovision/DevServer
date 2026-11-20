"""``reservations-chunk`` job: fair-share slot, then book each item or group.

Safety properties:

* **Idempotent reruns.** Each unit (a lone item or a whole group) is booked in
  its own transaction that starts with ``SELECT ... FOR UPDATE`` on the item
  rows and skips anything that is no longer ``pending``/``processing``. The
  reservation insert and the item's ``succeeded`` update commit together, so a
  rerun after a crash (PgQueuer ``retry_timer`` or the stuck-item sweep) never
  books twice.
* **No overbooking.** Property rows are locked ``FOR UPDATE`` in ascending id
  order before the overlap count (see :mod:`reservations.booking`).
* **Fair share.** At most ``CLIENT_MAX_CONCURRENT_CHUNKS`` chunks per client run
  at once across all workers, enforced with session advisory locks held on a
  dedicated connection for the life of the chunk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from pgqueuer.models import Job
from pgqueuer.queries import Queries

from reservations import batch_state, booking
from reservations.config import Settings
from reservations.logs import log_context
from reservations.queue import CHUNK_ENTRYPOINT

logger = logging.getLogger(__name__)

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.DeadlockDetectedError,
    asyncpg.SerializationError,
    asyncpg.PostgresConnectionError,
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.TooManyConnectionsError,
    ConnectionError,
    TimeoutError,
)

LIVE_ITEM_STATUSES = ("pending", "processing")


@dataclass
class WorkerDeps:
    pool: asyncpg.Pool
    settings: Settings
    queries: Queries
    http: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)


@dataclass
class ChunkStats:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


# ----------------------------------------------------------------- fair share


async def try_acquire_slot(conn: Any, client_id: str, slots: int) -> int | None:
    """Take the first free per-client slot lock, or return ``None`` if all are busy."""
    for slot in range(slots):
        if await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1), $2)", client_id, slot):
            return slot
    return None


async def release_slot(conn: Any, client_id: str, slot: int) -> None:
    try:
        await conn.execute("SELECT pg_advisory_unlock(hashtext($1), $2)", client_id, slot)
    except Exception:
        # Broken connection: the server session is gone, and the lock with it.
        # Make sure the pool does not hand this connection out again.
        logger.warning("could not release client slot; terminating connection", exc_info=True)
        with contextlib.suppress(Exception):
            conn.terminate()


# ------------------------------------------------------------------ job entry


async def process_chunk_job(job: Job, deps: WorkerDeps) -> None:
    data = json.loads(job.payload or b"{}")
    batch_id = UUID(data["batch_id"])
    client_id: str = data["client_id"]
    item_ids = [UUID(i) for i in data["item_ids"]]

    with log_context(job_id=job.id, batch_id=batch_id, client_id=client_id):
        async with deps.pool.acquire() as lock_conn:
            slot = await try_acquire_slot(
                lock_conn, client_id, deps.settings.client_max_concurrent_chunks
            )
            if slot is None:
                # Same pattern as RepoLockBusy in apps/worker: defer, return OK.
                (new_id,) = await deps.queries.enqueue(
                    CHUNK_ENTRYPOINT,
                    job.payload,
                    priority=job.priority,
                    execute_after=deps.settings.client_busy_delay,
                )
                logger.info("client busy, chunk deferred", extra={"deferred_job_id": new_id})
                return
            try:
                started = time.perf_counter()
                stats = await run_chunk(deps, batch_id, client_id, item_ids)
                logger.info(
                    "chunk processed",
                    extra={
                        "slot": slot,
                        "items": len(item_ids),
                        "succeeded": stats.succeeded,
                        "failed": stats.failed,
                        "skipped": stats.skipped,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            finally:
                await release_slot(lock_conn, client_id, slot)


async def run_chunk(
    deps: WorkerDeps, batch_id: UUID, client_id: str, item_ids: list[UUID]
) -> ChunkStats:
    stats = ChunkStats()
    units = await _start_chunk(deps.pool, batch_id, client_id, item_ids)
    for unit in units:
        succeeded, failed, skipped = await process_unit(deps, batch_id, client_id, unit)
        stats.succeeded += succeeded
        stats.failed += failed
        stats.skipped += skipped
    return stats


async def _start_chunk(
    pool: asyncpg.Pool, batch_id: UUID, client_id: str, item_ids: list[UUID]
) -> list[list[UUID]]:
    """Mark the batch and the chunk's pending items ``processing``; return units.

    Whole groups are always loaded, even if the payload only listed part of
    one, so a group is never booked partially.
    """
    async with pool.acquire() as conn, conn.transaction():
        # Batch row first: same lock order as the cancel endpoint.
        batch = await conn.fetchrow(
            "SELECT id FROM reservation_batches WHERE id = $1 AND client_id = $2 FOR UPDATE",
            batch_id,
            client_id,
        )
        if batch is None:
            logger.warning("chunk references unknown batch; dropping")
            return []
        rows = await conn.fetch(
            """
            SELECT id, group_id, status FROM reservation_batch_items
             WHERE batch_id = $1 AND client_id = $2
               AND (id = ANY($3::uuid[])
                    OR group_id IN (SELECT group_id FROM reservation_batch_items
                                     WHERE batch_id = $1 AND id = ANY($3::uuid[])
                                       AND group_id IS NOT NULL))
             ORDER BY seq
            """,
            batch_id,
            client_id,
            item_ids,
        )
        live = [r for r in rows if r["status"] in LIVE_ITEM_STATUSES]
        if not live:
            return []
        await conn.execute(
            """
            UPDATE reservation_batches
               SET status = 'processing', started_at = coalesce(started_at, now())
             WHERE id = $1 AND status = 'accepted'
            """,
            batch_id,
        )
        await conn.execute(
            """
            UPDATE reservation_batch_items SET status = 'processing', updated_at = now()
             WHERE id = ANY($1::uuid[]) AND status = 'pending'
            """,
            [r["id"] for r in live],
        )

    units: list[list[UUID]] = []
    groups: dict[str, list[UUID]] = {}
    for r in live:
        if r["group_id"] is None:
            units.append([r["id"]])
        elif r["group_id"] in groups:
            groups[r["group_id"]].append(r["id"])
        else:
            groups[r["group_id"]] = [r["id"]]
            units.append(groups[r["group_id"]])
    return units


# ------------------------------------------------------------------- one unit


async def process_unit(
    deps: WorkerDeps, batch_id: UUID, client_id: str, unit: list[UUID]
) -> tuple[int, int, int]:
    """Book one item or one group, retrying transient errors. Returns counts."""
    settings = deps.settings
    attempt = 0
    while True:
        attempt += 1
        try:
            return await _book_unit(deps.pool, batch_id, client_id, unit)
        except TRANSIENT_ERRORS as exc:
            with contextlib.suppress(*TRANSIENT_ERRORS):
                await _bump_attempts(deps.pool, unit)
            if attempt >= settings.item_max_attempts:
                logger.error(
                    "item retries exhausted",
                    extra={"item_ids": [str(i) for i in unit], "error": repr(exc)},
                )
                return await _fail_unit(
                    deps.pool,
                    batch_id,
                    client_id,
                    unit,
                    booking.INTERNAL_RETRY_EXHAUSTED,
                    f"transient error after {attempt} attempts: {exc!r}",
                )
            delay = settings.item_retry_base_s * (2 ** (attempt - 1))
            delay += random.uniform(0, delay)  # jitter
            logger.warning(
                "transient error, retrying item",
                extra={"attempt": attempt, "delay_s": round(delay, 3), "error": repr(exc)},
            )
            await asyncio.sleep(delay)
        except Exception as exc:
            # A bug or corrupt payload: fail the unit so the batch can finish.
            logger.exception("unexpected error booking item")
            return await _fail_unit(
                deps.pool, batch_id, client_id, unit, booking.INTERNAL_ERROR, repr(exc)
            )


async def _lock_live_items(
    conn: Any, batch_id: UUID, client_id: str, unit: list[UUID]
) -> list[Any]:
    rows = await conn.fetch(
        """
        SELECT id, status, payload::text AS payload FROM reservation_batch_items
         WHERE id = ANY($1::uuid[]) AND batch_id = $2 AND client_id = $3
         ORDER BY seq
           FOR UPDATE
        """,
        unit,
        batch_id,
        client_id,
    )
    return [r for r in rows if r["status"] in LIVE_ITEM_STATUSES]


async def _book_unit(
    pool: asyncpg.Pool, batch_id: UUID, client_id: str, unit: list[UUID]
) -> tuple[int, int, int]:
    async with pool.acquire() as conn, conn.transaction():
        # 1. Lock the items; skip what an earlier attempt or a cancel already settled.
        live = await _lock_live_items(conn, batch_id, client_id, unit)
        skipped = len(unit) - len(live)
        if not live:
            return 0, 0, skipped
        requests = [
            booking.BookingRequest.from_item(r["id"], client_id, r["payload"]) for r in live
        ]

        # 2. Lock properties in ascending id order (level-3 throttle + correctness).
        capacities = await booking.lock_properties(
            conn, client_id, (r.property_id for r in requests)
        )

        # 3-5. Book every member inside a savepoint: a group is all or nothing.
        booked: dict[UUID, UUID] = {}
        culprit: tuple[UUID, booking.BookingError] | None = None
        try:
            async with conn.transaction():
                for req in requests:
                    try:
                        booked[req.item_id] = await booking.book(conn, req, capacities)
                    except booking.BookingError as err:
                        culprit = (req.item_id, err)
                        raise
        except booking.BookingError:
            booked = {}

        # 6. Item status + batch counters in the same transaction.
        if culprit is None:
            await conn.executemany(
                """
                UPDATE reservation_batch_items
                   SET status = 'succeeded', reservation_id = $2, attempts = attempts + 1,
                       error_code = NULL, error_message = NULL, error_details = NULL,
                       updated_at = now()
                 WHERE id = $1
                """,
                list(booked.items()),
            )
            await batch_state.record_outcomes(conn, batch_id, succeeded=len(booked))
            return len(booked), 0, skipped

        culprit_id, err = culprit
        group = len(requests) > 1
        failures = []
        for req in requests:
            if req.item_id == culprit_id:
                failures.append((req.item_id, err.code, err.message, err.details))
            else:
                failures.append(
                    (
                        req.item_id,
                        booking.GROUP_FAILED,
                        f"group {req.group_id!r} rolled back: {err.code} on another member",
                        {"caused_by_item_id": str(culprit_id), "caused_by_code": err.code},
                    )
                )
        await _mark_failed(conn, failures, count_attempt=True)
        await batch_state.record_outcomes(conn, batch_id, failed=len(failures))
        if group:
            logger.info("group failed", extra={"group_id": requests[0].group_id, "code": err.code})
        return 0, len(failures), skipped


async def _mark_failed(
    conn: Any,
    failures: list[tuple[UUID, str, str, dict[str, Any] | None]],
    *,
    count_attempt: bool,
) -> None:
    await conn.executemany(
        f"""
        UPDATE reservation_batch_items
           SET status = 'failed', error_code = $2, error_message = $3, error_details = $4::jsonb,
               reservation_id = NULL, updated_at = now()
               {", attempts = attempts + 1" if count_attempt else ""}
         WHERE id = $1
        """,
        [(i, code, msg, json.dumps(d) if d is not None else None) for i, code, msg, d in failures],
    )


async def _fail_unit(
    pool: asyncpg.Pool, batch_id: UUID, client_id: str, unit: list[UUID], code: str, message: str
) -> tuple[int, int, int]:
    async with pool.acquire() as conn, conn.transaction():
        live = await _lock_live_items(conn, batch_id, client_id, unit)
        if not live:
            return 0, 0, len(unit)
        await _mark_failed(
            conn, [(r["id"], code, message, None) for r in live], count_attempt=False
        )
        await batch_state.record_outcomes(conn, batch_id, failed=len(live))
        return 0, len(live), len(unit) - len(live)


async def _bump_attempts(pool: asyncpg.Pool, unit: list[UUID]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE reservation_batch_items SET attempts = attempts + 1, updated_at = now() "
            "WHERE id = ANY($1::uuid[]) AND status IN ('pending', 'processing')",
            unit,
        )
