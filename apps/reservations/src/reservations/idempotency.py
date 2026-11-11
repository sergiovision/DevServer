"""Idempotency-Key handling: canonical request hash and replay lookup."""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_KEY_LENGTH = 128


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_hash(data: Any) -> str:
    """sha256 of the canonical JSON body (key order and whitespace do not matter)."""
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def valid_key(key: str | None) -> bool:
    return key is not None and 1 <= len(key) <= MAX_KEY_LENGTH


async def find_batch(conn: Any, client_id: str, key: str) -> Any:
    return await conn.fetchrow(
        "SELECT id, request_hash FROM reservation_batches "
        "WHERE client_id = $1 AND idempotency_key = $2",
        client_id,
        key,
    )
