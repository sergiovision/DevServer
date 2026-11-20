"""``reservations-webhook`` job: signed POST of the batch summary to callback_url.

Each attempt is recorded in ``webhook_deliveries`` (a partial unique index
allows at most one success per batch). A failed attempt re-enqueues the next
one with backoff, in the same transaction as the attempt record. Webhook
failures never touch the batch status.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from uuid import UUID

import httpx
from pgqueuer.models import Job

from reservations.logs import log_context
from reservations.queue import enqueue_webhook
from reservations.worker.process_chunk import WorkerDeps

logger = logging.getLogger(__name__)


def sign(secret: str, timestamp: str, body: str) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def batch_summary(row: Any) -> dict[str, Any]:
    def iso(v: Any) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "batch_id": str(row["id"]),
        "status": row["status"],
        "total_items": row["total_items"],
        "succeeded": row["succeeded"],
        "failed": row["failed"],
        "cancelled": row["cancelled"],
        "created_at": iso(row["created_at"]),
        "started_at": iso(row["started_at"]),
        "finished_at": iso(row["finished_at"]),
    }


async def deliver_webhook_job(job: Job, deps: WorkerDeps) -> None:
    data = json.loads(job.payload or b"{}")
    batch_id = UUID(data["batch_id"])
    client_id: str = data["client_id"]
    attempt = int(data.get("attempt", 1))
    settings = deps.settings

    with log_context(job_id=job.id, batch_id=batch_id, client_id=client_id):
        async with deps.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM reservation_batches WHERE id = $1 AND client_id = $2",
                batch_id,
                client_id,
            )
            if row is None or not row["callback_url"]:
                return
            already = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM webhook_deliveries WHERE batch_id = $1 AND success)",
                batch_id,
            )
            if already:
                return  # rerun after a crash: already delivered

        body = json.dumps(batch_summary(row), separators=(",", ":"), sort_keys=True)
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Signature": sign(settings.webhook_secret, timestamp, body),
            "X-Timestamp": timestamp,
            "X-Batch-Id": str(batch_id),
        }
        status_code: int | None = None
        error: str | None = None
        try:
            resp = await deps.http.post(
                row["callback_url"],
                content=body,
                headers=headers,
                timeout=settings.webhook_timeout_s,
            )
            status_code = resp.status_code
            if not 200 <= resp.status_code < 300:
                error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            error = repr(exc)
        success = error is None

        async with deps.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO webhook_deliveries (batch_id, client_id, attempt, status_code, success, error)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (batch_id) WHERE success DO NOTHING
                """,
                batch_id,
                client_id,
                attempt,
                status_code,
                success,
                error,
            )
            if success:
                logger.info(
                    "webhook delivered", extra={"attempt": attempt, "status_code": status_code}
                )
                return
            if attempt >= settings.webhook_max_attempts:
                logger.error(
                    "webhook delivery failed permanently; giving up",
                    extra={"attempt": attempt, "error": error},
                )
                return
            delay = settings.webhook_delay_after(attempt)
            await enqueue_webhook(conn, batch_id, client_id, attempt + 1, execute_after=delay)
            logger.warning(
                "webhook delivery failed; retry scheduled",
                extra={"attempt": attempt, "error": error, "retry_in_s": delay.total_seconds()},
            )
