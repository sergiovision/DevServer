"""Reservation batches: async submit (202), status, items, cancel; reservations."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from reservations import batch_state, idempotency, ratelimit
from reservations.api.deps import AppSettings, ClientId, Pool
from reservations.api.problems import ProblemError, problem, request_id, validation_errors
from reservations.chunking import ChunkItem, plan_chunks
from reservations.logs import log_context
from reservations.queue import enqueue_chunks
from reservations.schemas import (
    BatchCounts,
    BatchCreate,
    BatchItemOut,
    BatchItemsPage,
    BatchLinks,
    BatchOut,
    ReservationOut,
    semantic_errors,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reservation-batches"])


class _LostRaceError(Exception):
    """A concurrent request with the same Idempotency-Key committed first."""


# ------------------------------------------------------------------ helpers


async def _batch_out(conn: Any, client_id: str, batch_id: UUID) -> BatchOut | None:
    row = await conn.fetchrow(
        "SELECT * FROM reservation_batches WHERE id = $1 AND client_id = $2", batch_id, client_id
    )
    if row is None:
        return None
    by_status = {
        r["status"]: r["n"]
        for r in await conn.fetch(
            "SELECT status, count(*) AS n FROM reservation_batch_items "
            "WHERE batch_id = $1 AND status IN ('pending', 'processing') GROUP BY status",
            batch_id,
        )
    }
    base = f"/v1/reservation-batches/{row['id']}"
    return BatchOut(
        request_id=request_id(),
        batch_id=row["id"],
        status=row["status"],
        total_items=row["total_items"],
        counts=BatchCounts(
            succeeded=row["succeeded"],
            failed=row["failed"],
            cancelled=row["cancelled"],
            pending=by_status.get("pending", 0),
            processing=by_status.get("processing", 0),
        ),
        callback_url=row["callback_url"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        links=BatchLinks(self=base, items=f"{base}/items"),
    )


async def _replay(conn: Any, client_id: str, existing: Any, body_hash: str) -> JSONResponse:
    if existing["request_hash"] != body_hash:
        raise ProblemError(
            409,
            "Idempotency-Key was already used with a different request body",
            type_="urn:problem:idempotency-key-reused",
            batch_id=str(existing["id"]),
        )
    out = await _batch_out(conn, client_id, existing["id"])
    assert out is not None
    return JSONResponse(
        out.model_dump(mode="json"),
        status_code=200,
        headers={"Location": out.links.self, "Idempotent-Replayed": "true"},
    )


def _parse_body(raw: bytes, max_items: int, chunk_size: int) -> tuple[Any, BatchCreate]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProblemError(400, f"body is not valid JSON: {exc}") from exc
    items = data.get("items") if isinstance(data, dict) else None
    if isinstance(items, list) and not 1 <= len(items) <= max_items:
        raise ProblemError(
            413,
            f"a batch must contain between 1 and {max_items} items (got {len(items)}); "
            f"split larger submissions into several batches",
            type_="urn:problem:too-many-items",
            max_items=max_items,
        )
    try:
        batch = BatchCreate.model_validate(data)
    except ValidationError as exc:
        raise ProblemError(
            422,
            "request validation failed",
            type_="urn:problem:validation",
            errors=validation_errors(list(exc.errors())),
        ) from exc
    errors = semantic_errors(batch, chunk_size=chunk_size)
    if errors:
        raise ProblemError(
            422, "request validation failed", type_="urn:problem:validation", errors=errors
        )
    return data, batch


# ------------------------------------------------------------------- routes


@router.post(
    "/reservation-batches",
    status_code=202,
    response_model=BatchOut,
    responses={200: {"model": BatchOut, "description": "Idempotent replay"}},
)
async def create_batch(
    request: Request,
    client_id: ClientId,
    pool: Pool,
    settings: AppSettings,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    if not idempotency.valid_key(idempotency_key):
        raise ProblemError(400, "Idempotency-Key header is required (1-128 characters)")
    assert idempotency_key is not None
    raw = await request.body()  # size already capped by the middleware
    data, batch = _parse_body(raw, settings.max_items_per_batch, settings.chunk_size)
    body_hash = idempotency.request_hash(data)

    async with pool.acquire() as conn:
        existing = await idempotency.find_batch(conn, client_id, idempotency_key)
        if existing is not None:
            return await _replay(conn, client_id, existing, body_hash)

        batch_id = uuid.uuid4()
        try:
            async with conn.transaction():
                # Counters first; any failure below rolls them back with everything else.
                await ratelimit.consume(
                    conn,
                    client_id,
                    ratelimit.BATCHES_PER_MINUTE,
                    "minute",
                    1,
                    settings.batches_per_minute_per_client,
                )
                await ratelimit.consume(
                    conn,
                    client_id,
                    ratelimit.ITEMS_PER_DAY,
                    "day",
                    len(batch.items),
                    settings.items_per_day_per_client,
                )
                inserted = await conn.fetchval(
                    """
                    INSERT INTO reservation_batches
                        (id, client_id, idempotency_key, request_hash, total_items, callback_url)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (client_id, idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    batch_id,
                    client_id,
                    idempotency_key,
                    body_hash,
                    len(batch.items),
                    str(batch.callback_url) if batch.callback_url else None,
                )
                if inserted is None:
                    raise _LostRaceError
                chunk_items: list[ChunkItem] = []
                ids: list[UUID] = []
                refs: list[str] = []
                groups: list[str | None] = []
                payloads: list[str] = []
                for seq, item in enumerate(batch.items):
                    item_id = uuid.uuid4()
                    ids.append(item_id)
                    refs.append(item.client_reference)
                    groups.append(item.group_id)
                    payloads.append(item.model_dump_json())
                    chunk_items.append(ChunkItem(item_id, seq, item.group_id))
                await conn.execute(
                    """
                    INSERT INTO reservation_batch_items
                        (id, batch_id, client_id, seq, client_reference, group_id, payload)
                    SELECT u.id, $1::uuid, $2::text, u.seq - 1, u.ref, u.grp, u.payload::jsonb
                      FROM unnest($3::uuid[], $4::text[], $5::text[], $6::text[])
                           WITH ORDINALITY AS u(id, ref, grp, payload, seq)
                    """,
                    batch_id,
                    client_id,
                    ids,
                    refs,
                    groups,
                    payloads,
                )
                await ratelimit.maybe_cleanup(conn, client_id)
                chunks = plan_chunks(chunk_items, settings.chunk_size)
                # Same connection, same transaction: batch and jobs commit together.
                job_ids = await enqueue_chunks(conn, batch_id, client_id, chunks)
        except _LostRaceError:
            existing = await idempotency.find_batch(conn, client_id, idempotency_key)
            assert existing is not None
            return await _replay(conn, client_id, existing, body_hash)
        except ratelimit.RateLimitExceededError as exc:
            # The winner of a same-key race may have used the last unit of quota.
            existing = await idempotency.find_batch(conn, client_id, idempotency_key)
            if existing is not None:
                return await _replay(conn, client_id, existing, body_hash)
            return problem(
                429,
                f"rate limit {exc.bucket} exceeded ({exc.limit}); retry in {exc.reset_s}s",
                type_="urn:problem:rate-limited",
                headers=exc.headers(),
                bucket=exc.bucket,
            )

        with log_context(batch_id=batch_id):
            logger.info(
                "batch accepted",
                extra={"items": len(batch.items), "chunks": len(chunks), "jobs": len(job_ids)},
            )
        out = await _batch_out(conn, client_id, batch_id)
    assert out is not None
    return JSONResponse(
        out.model_dump(mode="json"), status_code=202, headers={"Location": out.links.self}
    )


@router.get("/reservation-batches/{batch_id}", response_model=BatchOut)
async def get_batch(batch_id: UUID, client_id: ClientId, pool: Pool) -> BatchOut:
    async with pool.acquire() as conn:
        out = await _batch_out(conn, client_id, batch_id)
    if out is None:
        raise ProblemError(404, "batch not found")
    return out


@router.get("/reservation-batches/{batch_id}/items", response_model=BatchItemsPage)
async def list_batch_items(
    batch_id: UUID,
    client_id: ClientId,
    pool: Pool,
    status: Literal["pending", "processing", "succeeded", "failed", "cancelled"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(pattern=r"^-?\d{1,10}$")] = None,
) -> BatchItemsPage:
    after = int(cursor) if cursor is not None else -1
    async with pool.acquire() as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM reservation_batches WHERE id = $1 AND client_id = $2",
            batch_id,
            client_id,
        )
        if owned is None:
            raise ProblemError(404, "batch not found")
        rows = await conn.fetch(
            """
            SELECT seq, client_reference, group_id, status, reservation_id, error_code,
                   error_message, error_details::text AS error_details, attempts
              FROM reservation_batch_items
             WHERE batch_id = $1 AND client_id = $2 AND seq > $3
               AND ($4::text IS NULL OR status = $4)
             ORDER BY seq
             LIMIT $5
            """,
            batch_id,
            client_id,
            after,
            status,
            limit + 1,
        )
    page = rows[:limit]
    items = []
    for r in page:
        fields = dict(r)
        details = fields.pop("error_details")
        items.append(BatchItemOut(**fields, error_details=json.loads(details) if details else None))
    next_cursor = str(page[-1]["seq"]) if len(rows) > limit else None
    return BatchItemsPage(
        request_id=request_id(), batch_id=batch_id, items=items, next_cursor=next_cursor
    )


@router.post("/reservation-batches/{batch_id}/cancel", response_model=BatchOut)
async def cancel_batch(batch_id: UUID, client_id: ClientId, pool: Pool) -> BatchOut:
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Batch row first, then items: same lock order as the worker's chunk start.
            found = await conn.fetchval(
                "SELECT 1 FROM reservation_batches WHERE id = $1 AND client_id = $2 FOR UPDATE",
                batch_id,
                client_id,
            )
            if found is None:
                raise ProblemError(404, "batch not found")
            result = await conn.execute(
                """
                UPDATE reservation_batch_items
                   SET status = 'cancelled', updated_at = now()
                 WHERE batch_id = $1 AND client_id = $2 AND status = 'pending'
                """,
                batch_id,
                client_id,
            )
            cancelled = int(result.split()[-1])
            await batch_state.record_outcomes(conn, batch_id, cancelled=cancelled)
        if cancelled:
            with log_context(batch_id=batch_id):
                logger.info("batch items cancelled", extra={"cancelled": cancelled})
        out = await _batch_out(conn, client_id, batch_id)
    assert out is not None
    return out


@router.get("/reservations/{reservation_id}", response_model=ReservationOut)
async def get_reservation(reservation_id: UUID, client_id: ClientId, pool: Pool) -> ReservationOut:
    row = await pool.fetchrow(
        """
        SELECT id, property_id, client_reference, guest_name, guests, check_in, check_out,
               status, batch_item_id, created_at
          FROM reservations WHERE id = $1 AND client_id = $2
        """,
        reservation_id,
        client_id,
    )
    if row is None:
        raise ProblemError(404, "reservation not found")
    return ReservationOut(request_id=request_id(), **dict(row))
