"""Batch counters and the transition to a final status.

Every item outcome is recorded with :func:`record_outcomes` in the same
transaction that changed the items. Its ``UPDATE`` takes the batch row lock and,
under READ COMMITTED, re-reads the latest committed counters, so concurrent
chunks of one batch can never both miss (or both perform) finalization.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from reservations.queue import enqueue_webhook

logger = logging.getLogger(__name__)

_FINALIZE_SQL = """
UPDATE reservation_batches
   SET status = CASE
                  WHEN cancelled > 0 AND succeeded = 0 THEN 'cancelled'
                  WHEN failed = 0 THEN 'completed'
                  ELSE 'completed_with_errors'
                END,
       finished_at = now()
 WHERE id = $1
   AND status NOT IN ('completed', 'completed_with_errors', 'cancelled')
   AND succeeded + failed + cancelled >= total_items
RETURNING status, client_id, callback_url
"""


async def record_outcomes(
    conn: Any,
    batch_id: UUID,
    *,
    succeeded: int = 0,
    failed: int = 0,
    cancelled: int = 0,
) -> str | None:
    """Add to the batch counters; finalize the batch if nothing is left.

    Must run inside the caller's transaction. Returns the final status if this
    call finalized the batch, else ``None``.
    """
    if succeeded == failed == cancelled == 0:
        return None
    done = await conn.fetchval(
        """
        UPDATE reservation_batches
           SET succeeded = succeeded + $2, failed = failed + $3, cancelled = cancelled + $4
         WHERE id = $1
        RETURNING succeeded + failed + cancelled >= total_items
        """,
        batch_id,
        succeeded,
        failed,
        cancelled,
    )
    if not done:
        return None
    # Conditional update: exactly one transaction wins the transition, so
    # exactly one webhook job is enqueued (in this same transaction).
    row = await conn.fetchrow(_FINALIZE_SQL, batch_id)
    if row is None:
        return None
    if row["callback_url"]:
        await enqueue_webhook(conn, batch_id, row["client_id"], attempt=1)
    logger.info("batch finalized", extra={"batch_id": str(batch_id), "status": row["status"]})
    return row["status"]
