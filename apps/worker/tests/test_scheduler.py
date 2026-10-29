"""Tests for the background scheduler (services/scheduler.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import services.scheduler as scheduler
from services.scheduler import start_scheduler, stop_scheduler

BUILT_IN_JOBS = {"stale_task_recovery"}


@pytest.fixture(autouse=True)
def _no_db_schedules():
    """The lifecycle tests are about built-in jobs, not the schedules table."""
    with patch.object(scheduler, "_register_db_schedules", AsyncMock(return_value=0)):
        yield


class TestSchedulerLifecycle:
    async def test_start_creates_one_loop_per_built_in_job(self):
        tasks = await start_scheduler()

        assert set(scheduler._JOBS) == BUILT_IN_JOBS
        assert len(tasks) == len(BUILT_IN_JOBS)
        assert all(isinstance(t, asyncio.Task) and not t.done() for t in tasks)
        await stop_scheduler()

    async def test_stop_cancels_tasks(self):
        tasks = await start_scheduler()

        await stop_scheduler()

        assert all(t.done() for t in tasks)

    async def test_stop_noop_when_empty(self):
        scheduler._JOBS.clear()

        await stop_scheduler()  # must not raise

    async def test_start_clears_previous_tasks(self):
        await start_scheduler()
        tasks = await start_scheduler()

        assert len(tasks) == len(BUILT_IN_JOBS)
        await stop_scheduler()
