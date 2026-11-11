"""PgQueuer entrypoints and enqueue helpers.

Enqueueing goes through ``Queries.from_asyncpg_connection(conn)``: the pinned
PgQueuer 0.26.3 asyncpg driver runs ``INSERT INTO pgqueuer ...`` on exactly the
connection it is given, so inside ``conn.transaction()`` the jobs commit or roll
back together with the domain rows (verified in the installed package source,
``pgqueuer/adapters/drivers/asyncpg.py``). No outbox table is needed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import asyncpg
from pgqueuer.models import JobId
from pgqueuer.queries import Queries

CHUNK_ENTRYPOINT = "reservations-chunk"
WEBHOOK_ENTRYPOINT = "reservations-webhook"


def queries_for(conn: Any) -> Queries:
    """Queries bound to one connection (and therefore to its open transaction)."""
    return Queries.from_asyncpg_connection(cast(asyncpg.Connection, conn))


def chunk_payload(batch_id: UUID, client_id: str, item_ids: Sequence[UUID]) -> bytes:
    return json.dumps(
        {"batch_id": str(batch_id), "client_id": client_id, "item_ids": [str(i) for i in item_ids]}
    ).encode()


async def enqueue_chunks(
    conn: Any, batch_id: UUID, client_id: str, chunks: Sequence[Sequence[UUID]]
) -> list[JobId]:
    if not chunks:
        return []
    return await queries_for(conn).enqueue(
        [CHUNK_ENTRYPOINT] * len(chunks),
        [chunk_payload(batch_id, client_id, c) for c in chunks],
        [0] * len(chunks),
    )


async def enqueue_webhook(
    conn: Any,
    batch_id: UUID,
    client_id: str,
    attempt: int,
    execute_after: timedelta | None = None,
) -> list[JobId]:
    payload = json.dumps(
        {"batch_id": str(batch_id), "client_id": client_id, "attempt": attempt}
    ).encode()
    return await queries_for(conn).enqueue(
        WEBHOOK_ENTRYPOINT, payload, priority=0, execute_after=execute_after
    )
