"""The end of a task run: terminal status, daily stats and notification.

Whatever path a run took — first try, retries, the backup model — it ends
here exactly once. Keeping these writes in one module is what stops the
"success" bookkeeping from being copied into every branch of the runner.
"""

from __future__ import annotations

import logging
import traceback
from collections import Counter

from models.base import async_session
from services import memory, patch_export
from services.notify import notify

from . import store
from .context import TaskContext
from .results import AttemptResult, Success, Verdict

logger = logging.getLogger(__name__)


async def record(ctx: TaskContext, result: AttemptResult) -> bool:
    """Write the terminal state for ``result``; return whether the task succeeded."""
    match result.verdict:
        case Verdict.SUCCEEDED:
            assert result.success is not None
            await _record_success(ctx, result.success)
            return True
        case Verdict.SUSPENDED:
            # The side-effect gate already parked the task as blocked; the
            # gate's resolution re-enqueues it. Nothing to count yet.
            return False
        case Verdict.BLOCKED:
            await _record_terminal(ctx, "blocked")
            await notify.preflight_blocked(
                task_key=ctx.task.key, violations=list(result.violations)
            )
        case Verdict.BUDGET_EXCEEDED:
            await _record_terminal(ctx, "blocked")
            await notify.budget_exceeded(
                task_key=ctx.task.key,
                repo_name=ctx.repo.name,
                reason=result.reason,
                cum_cost=ctx.spend.cost_usd,
                cum_wall_ms=ctx.spend.wall_ms,
            )
        case Verdict.RETRY | Verdict.ESCALATE:
            await _record_terminal(ctx, "failed")
            await notify.task_failed(
                task_key=ctx.task.key,
                repo_name=ctx.repo.name,
                error_context=ctx.error_context,
                attempts=ctx.repo.max_retries,
                cost=ctx.spend.cost_usd,
            )
    return False


async def _record_terminal(ctx: TaskContext, status: str) -> None:
    await store.set_task_status(ctx.task.id, status)
    await store.add_daily_stat(failed=1)


async def _record_success(ctx: TaskContext, success: Success) -> None:
    metrics = success.metrics
    await store.set_task_status(ctx.task.id, "test")
    await store.add_daily_stat(
        completed=1,
        cost_usd=metrics.cost_usd,
        duration_ms=metrics.duration_ms,
        turns=metrics.turns,
    )
    await notify.task_success(
        task_key=ctx.task.key,
        git_flow=ctx.git_flow,
        pr_url=success.handover.pr_url,
        attempts=metrics.attempt,
        turns=metrics.turns,
        cost=metrics.cost_usd,
        duration_ms=metrics.duration_ms,
        repo_name=ctx.repo.name,
    )
    if ctx.git_flow == "patch":
        await _export_patches(ctx)
    await _remember_success(ctx, success)


async def _export_patches(ctx: TaskContext) -> None:
    """Export the task's commits as patch files; failure never demotes success."""
    base = ctx.repo.default_branch
    if ctx.local_original_branch and ctx.local_original_branch != ctx.branch_name:
        base = ctx.local_original_branch  # local repos branch off whatever was checked out
    try:
        patches = await patch_export.export_patches(
            task_key=ctx.task.key, repo_path=ctx.worktree_path, base=base
        )
    except (patch_export.InvalidPatchPath, RuntimeError, OSError) as exc:
        logger.warning("Patch export failed for %s: %s", ctx.task.key, exc)
        await store.emit_event(
            ctx.task.id, None, "patches_generated", {"ok": False, "error": str(exc)}
        )
        return
    await store.emit_event(
        ctx.task.id,
        None,
        "patches_generated",
        {"ok": True, "commits": patches.commits, "files": len(patches.files)},
    )
    ctx.log.note("patches", f"generated {len(patches.files)} files ({patches.commits} commits)")


async def record_blocked_before_attempts(ctx: TaskContext) -> None:
    """The run stopped before any attempt (abstain gate, rejected plan)."""
    await store.set_task_status(ctx.task.id, "blocked")


async def record_crash(task_id: int, task_key: str, exc: BaseException) -> None:
    logger.exception("Unhandled error in task %s", task_key)
    await store.set_task_status(task_id, "failed")
    await store.fail_open_runs(task_id, reason=f"crashed: {str(exc)[:400]}")
    await store.emit_event(
        task_id,
        None,
        "error",
        {"error": str(exc), "traceback": traceback.format_exc()[-3000:]},
    )
    await notify.text(f"❌ *{task_key}* crashed\n```\n{str(exc)[:300]}\n```")


async def record_cancelled(task_id: int) -> None:
    """Cancellation always leaves a terminal status behind."""
    await store.fail_open_runs(task_id, reason="Cancelled by user")
    await store.set_status_unless_moved_on(task_id, "cancelled")
    await store.emit_event(task_id, None, "status_change", {"status": "cancelled"})


def _topic(paths: tuple[str, ...]) -> str | None:
    """The most common top-level directory among the changed files."""
    tops = [
        segments[0]
        for path in paths
        if len(segments := path.strip().replace("\\", "/").lstrip("./").split("/")) > 1
        and segments[0]
    ]
    return Counter(tops).most_common(1)[0][0][:120] if tops else None


async def _remember_success(ctx: TaskContext, success: Success) -> None:
    """Store the run as an experience memory so similar tasks can recall it.

    Only successes are stored: the agent should not learn failed patterns.
    A memory failure never demotes a successful task.
    """
    task, metrics = ctx.task, success.metrics
    outcome_line = (
        f"completed by backup model {ctx.model}"
        if ctx.failover_label
        else f"completed in {metrics.attempt} attempt(s), {metrics.turns} turns, "
        f"PR: {success.handover.pr_url or 'none'}"
    )
    summary = (
        f"Task {task.key}: {task.title}\n"
        f"Description: {task.description[:300]}\n"
        f"Outcome: {outcome_line}\n"
        f"Result: {success.result_text[:600]}"
    )
    try:
        async with async_session() as db:
            await memory.store_memory(
                db,
                repo_id=ctx.repo.id,
                content=summary,
                memory_type="experience",
                task_id=task.id,
                topic=_topic(success.files_changed),
                metadata={
                    "task_key": task.key,
                    "attempts": metrics.attempt,
                    "turns": metrics.turns,
                    "pr_url": success.handover.pr_url,
                    "reality_score": ctx.evidence.reality_signal.get("score"),
                    "model": ctx.model,
                },
            )
    except Exception:
        # Memory is an optional enhancement (embedding model, pgvector); log
        # with the traceback and keep the task's success.
        logger.exception("Failed to store success memory for %s", task.key)
