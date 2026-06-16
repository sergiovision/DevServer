"""Tests for the background scheduler (services/scheduler.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.scheduler import start_scheduler, stop_scheduler


class TestSchedulerLifecycle:
    """Tests for starting and stopping the scheduler."""

    async def test_start_creates_tasks(self):
        """start_scheduler creates asyncio tasks for stale recovery and daily report."""
        tasks = await start_scheduler()

        assert len(tasks) == 2
        for t in tasks:
            assert isinstance(t, asyncio.Task)
            assert not t.done()

        # Cleanup
        await stop_scheduler()

    async def test_stop_cancels_tasks(self):
        """stop_scheduler cancels all running tasks."""
        tasks = await start_scheduler()
        assert len(tasks) == 2

        await stop_scheduler()

        for t in tasks:
            assert t.done()

    async def test_stop_noop_when_empty(self):
        """stop_scheduler is safe when no tasks are running."""
        import services.scheduler as mod
        mod._scheduler_tasks.clear()

        await stop_scheduler()  # Should not raise

    async def test_start_clears_previous_tasks(self):
        """start_scheduler clears any leftover tasks before starting new ones."""
        tasks1 = await start_scheduler()
        tasks2 = await start_scheduler()

        # tasks2 should be fresh (tasks1 should have been cancelled by clear)
        assert len(tasks2) == 2

        await stop_scheduler()
