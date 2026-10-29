"""Agent runner: the lifecycle of one coding task, start to finish.

lock → workspace → evidence (→ plan gate) → attempts (→ backup model)
     → outcome → tidy-up

This module only orchestrates. The phases live in :mod:`services.runner`;
conversational task types (skill, research) and repo-less tasks go to
:mod:`services.skill_runner` instead.
"""

from __future__ import annotations

import asyncio
import logging

from config import settings
from services import repo_lock, skill_runner
from services.runner import outcome, phases
from services.runner.context import (
    RepoSpec,
    TaskContext,
    TaskSpec,
    load_specs,
    resolve_git_flow,
    resolve_max_turns,
)
from services.runner.store import consume_continuation
from services.runner.task_log import TaskLog

logger = logging.getLogger(__name__)

_LIGHTWEIGHT_TYPES = ("skill", "research")


async def run_task(task_id: int, claude_mode: str = "max", max_turns: int | None = None) -> bool:
    """Run task ``task_id`` to a terminal state. Returns True on success.

    Raises :class:`services.repo_lock.RepoLockBusy` without touching the
    task when another task holds the repository; the caller re-queues it.
    """
    specs = await load_specs(task_id)
    if specs is None:
        return False
    task, repo, repo_model = specs
    if task.task_type in _LIGHTWEIGHT_TYPES or repo is None:
        return await skill_runner.run_lightweight_task(
            task_id=task_id, task_type=task.task_type, claude_mode=claude_mode, max_turns=max_turns
        )

    async with repo_lock.hold(repo.name, owner=task.key):
        with TaskLog(settings.log_dir, task.key) as log:
            ctx = TaskContext(
                task=task,
                repo=repo,
                billing_mode=claude_mode,
                max_turns=resolve_max_turns(max_turns, task.max_turns),
                log=log,
                model=task.model or repo_model,
                branch_name=f"agent/{task.key}".lower(),
                git_flow=resolve_git_flow(task, repo),
                compacted_context=task.compacted_context,
                auto_compacted=bool(task.compacted_context),
            )
            return await _run_locked(ctx)


async def _run_locked(ctx: TaskContext) -> bool:
    task = ctx.task
    _log_start(ctx.task, ctx.repo, ctx)
    ctx.log.started(task.key)
    if task.is_continuation:
        ctx.session_id = await consume_continuation(task.id)
        if ctx.session_id:
            ctx.log.note("continuation", f"resuming session {ctx.session_id}")

    success = False
    try:
        await phases.prepare_workspace(ctx)
        if not await phases.gather_evidence(ctx):
            return False
        success = await outcome.record(ctx, await phases.run_all_attempts(ctx))
    except asyncio.CancelledError:
        logger.warning("Task %s cancelled", task.key)
        await outcome.record_cancelled(task.id)
        raise
    except Exception as exc:
        # The last line of defence: any bug in a phase still ends the task in
        # a terminal state with the traceback on its event log.
        await outcome.record_crash(task.id, task.key, exc)
    finally:
        await _finalise_quietly(ctx, success=success)
    return success


async def _finalise_quietly(ctx: TaskContext, *, success: bool) -> None:
    """Tidy up without masking the run's own result or exception."""
    try:
        await phases.finalise(ctx, success=success)
    except RuntimeError, OSError:
        logger.exception("Cleanup after task %s failed", ctx.task.key)


def _log_start(task: TaskSpec, repo: RepoSpec, ctx: TaskContext) -> None:
    turns = str(ctx.max_turns) if ctx.max_turns is not None else "unlimited"
    logger.info(
        "=== Starting task: %s - %s (repo: %s, model: %s, max_turns: %s%s) ===",
        task.key,
        task.title,
        repo.name,
        ctx.model,
        turns,
        ", continuation" if task.is_continuation else "",
    )
