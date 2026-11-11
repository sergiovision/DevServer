"""Level-1 rate limits: fixed-window counters in Postgres.

:func:`consume` must run inside the transaction that creates the batch. If a
limit is exceeded it raises, the caller's transaction rolls back, and the
increment disappears with it ("roll the counter back"). The same rollback
undoes the increment when the request turns out to be an idempotent replay.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Literal

Unit = Literal["minute", "day"]

BATCHES_PER_MINUTE = "batches_per_minute"
ITEMS_PER_DAY = "items_per_day"


@dataclass
class RateLimitExceededError(Exception):
    bucket: str
    limit: int
    remaining: int
    reset_s: int

    def headers(self) -> dict[str, str]:
        return {
            "Retry-After": str(self.reset_s),
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset_s),
        }


async def consume(
    conn: Any, client_id: str, bucket: str, unit: Unit, amount: int, limit: int
) -> int:
    """Add ``amount`` to the current window; raise if it goes over ``limit``.

    Returns the remaining budget in the window.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO rate_limit_counters AS c (client_id, bucket, window_start, count)
        VALUES ($1, $2, date_trunc($3, now()), $4)
        ON CONFLICT (client_id, bucket, window_start)
        DO UPDATE SET count = c.count + EXCLUDED.count
        RETURNING c.count,
                  extract(epoch FROM c.window_start + ('1 ' || $3)::interval - clock_timestamp())
                      AS reset_s
        """,
        client_id,
        bucket,
        unit,
        amount,
    )
    count: int = row["count"]
    reset_s = max(1, math.ceil(float(row["reset_s"])))
    if count > limit:
        raise RateLimitExceededError(
            bucket=bucket,
            limit=limit,
            remaining=max(0, limit - (count - amount)),
            reset_s=reset_s,
        )
    return limit - count


async def maybe_cleanup(conn: Any, client_id: str, probability: float = 0.05) -> None:
    """Opportunistically drop this client's windows older than two days."""
    if random.random() < probability:
        await conn.execute(
            "DELETE FROM rate_limit_counters WHERE client_id = $1 "
            "AND window_start < now() - interval '2 days'",
            client_id,
        )
