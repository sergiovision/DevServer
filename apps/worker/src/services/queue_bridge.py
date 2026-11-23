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


#: Marks an enqueue as part of a post-completion chain. Without it the web
#: tier treats the enqueue as a fresh start and clears ``triggered_by_task_id``
#: so an old chain cannot count against a later manual run's depth cap.
TRIGGERED_ENQUEUE_HEADER = "X-DevServer-Triggered"


async def enqueue_via_web(task_id: int, *, triggered: bool = False) -> bool:
    """POST to the Next.js enqueue endpoint (single source of truth for the queue).

    Best-effort: returns ``True`` on a 200, ``False`` on any error. Callers
    decide whether a failed enqueue is fatal (the task row already exists in
    ``status='pending'`` and can be enqueued again). ``triggered`` is set only
    by ``services/post_completion`` so its trigger chain survives the enqueue.
    """
    headers = dict(settings.web_headers)
    if triggered:
        headers[TRIGGERED_ENQUEUE_HEADER] = "1"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"{settings.web_base_url}/api/tasks/{task_id}/enqueue",
                headers=headers,
            )
            return resp.status_code == 200
    except Exception:
        logger.exception("failed to enqueue task %d via web", task_id)
        return False
