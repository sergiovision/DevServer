"""Regression tests for the correctness fixes: locks, recovery, requeue, cancel."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from models.base import async_session
from services import queue_consumer, repo_lock, scheduler, task_control
from services.running_tasks import registry

from .conftest import Scenario
from .support import lock_owner, run_statuses, seed_task, task_status

pytestmark = pytest.mark.usefixtures("db_engine", "quiet_world")


async def _insert_lock(owner: str, *, expires_in: str = "1 hour", repo: str = "demo") -> None:
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO repo_locks (repo_name, task_key, expires_at) "
                f"VALUES (:repo, :owner, NOW() + interval '{expires_in}')"
            ),
            {"repo": repo, "owner": owner},
        )
        await db.commit()


class TestRepoLock:
    async def test_release_only_deletes_the_callers_own_lock(self):
        await _insert_lock("OTHER-1")
        async with async_session() as db:
            await repo_lock.release(db, repo_name="demo", owner="DEMO-1")
            await db.commit()
        assert await lock_owner() == "OTHER-1"

    async def test_expired_lock_can_be_taken_over(self):
        await _insert_lock("DEAD-1", expires_in="-1 minute")
        async with async_session() as db:
            assert await repo_lock.try_acquire(db, repo_name="demo", owner="DEMO-1")
        assert await lock_owner() == "DEMO-1"

    async def test_heartbeat_renews_the_lock_while_held(self, monkeypatch):
        monkeypatch.setattr(repo_lock, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(repo_lock, "LOCK_TTL", timedelta(seconds=1))
        async with repo_lock.hold("demo", owner="DEMO-1"):
            await asyncio.sleep(1.3)  # longer than the TTL
            async with async_session() as db:
                live = (
                    await db.execute(
                        text("SELECT expires_at > NOW() FROM repo_locks WHERE task_key = 'DEMO-1'")
                    )
                ).scalar_one()
            assert live
        assert await lock_owner() is None


class TestStaleRecovery:
    async def _make_stale(self, task_id: int) -> None:
        async with async_session() as db:
            await db.execute(
                text(
                    "UPDATE tasks SET status = 'running', "
                    "updated_at = NOW() - interval '2 hours' WHERE id = :id"
                ),
                {"id": task_id},
            )
            await db.commit()

    async def test_requeues_dead_task_and_releases_only_its_lock(self, git_repo, monkeypatch):
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr(scheduler, "enqueue_via_web", enqueue)
        monkeypatch.setattr(scheduler, "tg_send", AsyncMock())
        dead = await seed_task(git_repo, task_key="DEAD-1")
        await seed_task(git_repo, task_key="OTHER-1", repo_name="other")
        await self._make_stale(dead)
        await _insert_lock("DEAD-1", expires_in="-1 minute")
        await _insert_lock("OTHER-1", repo="other")

        await scheduler._run_stale_task_recovery()

        assert await task_status(dead) == "pending"
        enqueue.assert_awaited_once_with(dead)
        assert await lock_owner("demo") is None
        assert await lock_owner("other") == "OTHER-1"

    async def test_task_with_live_lock_is_not_stale(self, git_repo, monkeypatch):
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr(scheduler, "enqueue_via_web", enqueue)
        alive = await seed_task(git_repo, task_key="LIVE-1")
        await self._make_stale(alive)
        await _insert_lock("LIVE-1")

        await scheduler._run_stale_task_recovery()

        assert await task_status(alive) == "running"
        enqueue.assert_not_awaited()


def _job(task_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=7, priority=3, payload=json.dumps({"taskId": task_id}).encode())


class TestQueueConsumer:
    async def test_lock_contention_requeues_instead_of_failing(self, scenario: Scenario, git_repo):
        task_id = await seed_task(git_repo)
        await _insert_lock("OTHER-1")
        queries = SimpleNamespace(enqueue=AsyncMock(return_value=[99]))

        await queue_consumer.execute_job(_job(task_id), queries)  # must not raise

        kwargs = queries.enqueue.await_args.kwargs
        assert kwargs["execute_after"] == queue_consumer.LOCK_RETRY_DELAY
        async with async_session() as db:
            job_id = (
                await db.execute(
                    text("SELECT queue_job_id FROM tasks WHERE id = :id"), {"id": task_id}
                )
            ).scalar_one()
        assert job_id == "99"
        assert await task_status(task_id) == "queued"

    async def test_cancel_from_operator_stops_agent_within_ten_seconds(
        self, scenario: Scenario, git_repo: Path
    ):
        scenario.script({"exit": 0, "sleep": 30, "write_after_sleep": {"late.txt": "x"}})
        task_id = await seed_task(git_repo)
        job = asyncio.create_task(queue_consumer.execute_job(_job(task_id), SimpleNamespace()))
        async with asyncio.timeout(10):
            while not scenario.calls:
                await asyncio.sleep(0.05)

        async with asyncio.timeout(10):
            cancelled = await task_control.cancel(task_id=task_id)
            await job  # the job ends normally: an operator cancel is not a failure

        assert cancelled.stopped_running_agent
        assert await task_status(task_id) == "cancelled"
        assert await run_statuses(task_id) == ["failed"]
        assert not registry.is_running(task_id)
        assert not (git_repo / "late.txt").exists()

    async def test_cancelling_a_finished_task_is_refused(self, git_repo):
        task_id = await seed_task(git_repo, status="test")
        with pytest.raises(task_control.TaskStateError):
            await task_control.cancel(task_id=task_id)
