"""Registry of the task runs executing in this worker process.

Cancelling a task used to only flip its status in the database while the
agent CLI kept running to completion. The queue consumer now registers each
``run_task`` coroutine here, so a cancel request can reach the actual
``asyncio.Task``. Cancellation then propagates down to ``proc.run``, which
kills the agent's process group, and the runner records a terminal status.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: How long :meth:`RunningTasks.cancel` waits for the run to wind down.
CANCEL_WAIT_SECONDS = 10.0


class RunningTasks:
    def __init__(self) -> None:
        self._runs: dict[int, asyncio.Task[object]] = {}
        self._cancel_requested: set[int] = set()

    @contextlib.contextmanager
    def track(self, task_id: int, run: asyncio.Task[object]) -> Iterator[None]:
        """Register ``run`` as the execution of ``task_id`` for the block."""
        self._runs[task_id] = run
        try:
            yield
        finally:
            if self._runs.get(task_id) is run:
                del self._runs[task_id]
            self._cancel_requested.discard(task_id)

    def is_running(self, task_id: int) -> bool:
        return task_id in self._runs

    def was_cancel_requested(self, task_id: int) -> bool:
        return task_id in self._cancel_requested

    async def cancel(self, task_id: int, *, wait_seconds: float = CANCEL_WAIT_SECONDS) -> bool:
        """Cancel the run of ``task_id`` and wait for it to finish.

        Returns False when the task is not running in this process. Waiting
        means the caller can rely on the agent process being gone and the
        repo lock released once this returns (within ``wait_seconds``).
        """
        run = self._runs.get(task_id)
        if run is None:
            return False
        self._cancel_requested.add(task_id)
        run.cancel()
        done, _ = await asyncio.wait({run}, timeout=wait_seconds)
        if not done:
            logger.warning("Task %d did not stop within %.0fs of cancel", task_id, wait_seconds)
        return True


registry = RunningTasks()
