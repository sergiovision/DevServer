"""Enqueue handoff — the single source of truth for the queue lives in Next.js.

The ``pgqueuer`` producer is one SQL INSERT in ``apps/web/src/lib/queue.ts``
(``enqueueTask``). The worker never writes to that table directly; every
worker-side path that needs to *start* a task (goal-graph leaves, night cycle,
the MCP task-control endpoints) POSTs back to the Next.js enqueue endpoint so
the queue producer stays in exactly one place.
"""

from __future__ import annotations

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def enqueue_via_web(task_id: int) -> bool:
    """POST to the Next.js enqueue endpoint (single source of truth for the queue).

    Best-effort: returns ``True`` on a 200, ``False`` on any error. Callers
    decide whether a failed enqueue is fatal (the task row already exists in
    ``status='pending'`` and can be enqueued again).
    """
    web_port = settings.web_port  # Next.js port (WEB_PORT in .env, default 3200)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"http://localhost:{web_port}/api/tasks/{task_id}/enqueue"
            )
            return resp.status_code == 200
    except Exception:
        logger.exception("failed to enqueue task %d via web", task_id)
        return False
