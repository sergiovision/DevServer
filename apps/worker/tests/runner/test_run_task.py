"""Characterisation tests for ``agent_runner.run_task``.

Written against the pre-refactor runner and kept unchanged through the
refactor: they pin the emitted event sequence, the per-attempt run rows and
the final task status for each major path. ``patches_generated`` is ignored
in the sequences because patch export is a separate demo feature with its
own tests.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from models.base import async_session
from services.agent_runner import run_task
from services.repo_lock import RepoLockBusy

from .conftest import Scenario
from .support import (
    event_types,
    is_subsequence,
    lock_owner,
    run_statuses,
    seed_task,
    task_status,
)

pytestmark = pytest.mark.usefixtures("db_engine", "quiet_world")

_IGNORED_EVENTS = {"patches_generated", "log_line"}


async def core_events(task_id: int) -> list[str]:
    return [event for event in await event_types(task_id) if event not in _IGNORED_EVENTS]


async def test_success_on_first_attempt(scenario: Scenario, git_repo: Path) -> None:
    scenario.script({"exit": 0, "write": {"feature.txt": "hello\n"}, "session_id": "s1"})
    task_id = await seed_task(git_repo)

    assert await run_task(task_id) is True

    assert await task_status(task_id) == "test"
    assert await run_statuses(task_id) == ["success"]
    assert await core_events(task_id) == [
        "status_change",
        "repo_map_built",
        "reality_signal",
        "progress",
        "cost_update",
        "pr_preflight_pass",
    ]
    assert await lock_owner() is None


async def test_verify_failure_then_retry(scenario: Scenario, git_repo: Path) -> None:
    scenario.script(
        {"exit": 0, "session_id": "s1"},
        {"exit": 0, "write": {"ok.txt": "fixed\n"}, "session_id": "s1"},
    )
    task_id = await seed_task(git_repo, test_cmd="test -f ok.txt")

    assert await run_task(task_id) is True

    assert await task_status(task_id) == "test"
    assert await run_statuses(task_id) == ["failed", "success"]
    assert await core_events(task_id) == [
        "status_change",
        "repo_map_built",
        "reality_signal",
        "progress",
        "cost_update",
        "error",
        "progress",
        "cost_update",
        "pr_preflight_pass",
    ]
    # The resumed attempt is told why the previous one failed.
    retry_prompt = scenario.calls[1]["prompt"]
    assert retry_prompt.startswith("## Previous Attempt Failed")


async def test_primary_failure_falls_over_to_backup_model(
    scenario: Scenario, git_repo: Path
) -> None:
    scenario.script({"exit": 1}, {"exit": 1}, {"exit": 0, "write": {"feature.txt": "x\n"}})
    task_id = await seed_task(git_repo, backup_model="fake-backup")

    assert await run_task(task_id) is True

    assert [call["model"] for call in scenario.calls] == [
        "fake-primary",
        "fake-primary",
        "fake-backup",
    ]
    assert await task_status(task_id) == "test"
    assert await run_statuses(task_id) == ["failed", "failed", "success"]
    assert is_subsequence(
        ["status_change", "progress", "progress", "backup_model_switch", "progress"],
        await core_events(task_id),
    )


async def test_budget_exceeded_blocks_task(scenario: Scenario, git_repo: Path) -> None:
    scenario.script({"exit": 1, "cost_usd": 0.05})
    task_id = await seed_task(git_repo, max_cost_usd=Decimal("0.01"))

    assert await run_task(task_id, claude_mode="api") is False

    assert len(scenario.calls) == 1
    assert await task_status(task_id) == "blocked"
    assert await core_events(task_id) == [
        "status_change",
        "repo_map_built",
        "reality_signal",
        "progress",
        "budget_exceeded",
    ]


async def test_cancellation_kills_agent_and_marks_task_cancelled(
    scenario: Scenario, git_repo: Path
) -> None:
    scenario.script({"exit": 0, "sleep": 30, "write_after_sleep": {"late.txt": "too late\n"}})
    task_id = await seed_task(git_repo)

    running = asyncio.create_task(run_task(task_id))
    async with asyncio.timeout(10):
        while not scenario.calls:
            await asyncio.sleep(0.05)

    running.cancel()
    async with asyncio.timeout(10):
        with pytest.raises(asyncio.CancelledError):
            await running

    assert await task_status(task_id) == "cancelled"
    assert await run_statuses(task_id) == ["failed"]
    assert await lock_owner() is None
    await asyncio.sleep(0.5)
    assert not (git_repo / "late.txt").exists(), "agent process outlived the cancellation"


async def test_lock_contention_leaves_task_untouched(scenario: Scenario, git_repo: Path) -> None:
    task_id = await seed_task(git_repo)
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO repo_locks (repo_name, task_key, expires_at) "
                "VALUES ('demo', 'OTHER-1', NOW() + interval '1 hour')"
            )
        )
        await db.commit()

    with pytest.raises(RepoLockBusy):
        await run_task(task_id)

    assert scenario.calls == []
    assert await task_status(task_id) == "queued"
    assert await event_types(task_id) == []
    assert await lock_owner() == "OTHER-1"
