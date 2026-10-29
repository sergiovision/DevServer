"""One attempt: budget check → agent → verify → PR preflight → git handover.

Every exit from an attempt is an :class:`AttemptResult` whose verdict tells
the attempt loop what to do next. The attempt records its own ``task_runs``
row; the task's terminal status is left to :mod:`.outcome`.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from models.base import async_session
from services import git_ops, side_effect_gate, task_notes, verifier
from services.agent_backends import AgentBackend, AgentResult
from services.budget import BudgetState, check_budget
from services.notify import notify

from . import agent_call, store
from .context import TaskContext
from .delivery import AttemptMetrics, hand_over
from .failures import agent_failed, preflight_failed, verify_failed
from .preflight import PreflightVerdict, run_preflight
from .prompt import build_prompt
from .results import AttemptResult, Success, Verdict

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


async def run_attempt(
    ctx: TaskContext, backend: AgentBackend, *, attempt: int, is_backup: bool
) -> AttemptResult:
    budget_stop = await _check_budget(ctx, attempt=attempt)
    if budget_stop is not None:
        return budget_stop

    label = "Backup attempt" if is_backup else "Attempt"
    logger.info("--- %s %d/%d (%s) ---", label, attempt, ctx.repo.max_retries, ctx.model)
    run_id = await store.start_run(ctx.task.id, attempt=attempt, branch=ctx.branch_name)
    progress: dict = {"attempt": attempt, "max_retries": ctx.repo.max_retries}
    if is_backup:
        progress["backup_model"] = True
    await store.emit_event(ctx.task.id, run_id, "progress", progress)

    result, duration_ms = await _call_agent(ctx, backend, run_id=run_id)
    _record_spend(ctx, result, duration_ms)
    _log_agent_result(ctx, result, label=f"{label} {attempt}", duration_ms=duration_ms)

    if await _suspended_for_gate(ctx, run_id=run_id):
        return AttemptResult(Verdict.SUSPENDED)
    if result.exit_code != 0:
        return await agent_failed(
            ctx, backend, result, run_id=run_id, attempt=attempt, duration_ms=duration_ms
        )
    return await _verify_and_deliver(
        ctx, result, run_id=run_id, attempt=attempt, duration_ms=duration_ms
    )


async def _check_budget(ctx: TaskContext, *, attempt: int) -> AttemptResult | None:
    verdict = check_budget(
        cost_usd=ctx.spend.cost_usd, wall_ms=ctx.spend.wall_ms, limits=ctx.budget_limits
    )
    spent = {
        "cum_cost_usd": float(ctx.spend.cost_usd),
        "cum_wall_seconds": ctx.spend.wall_ms / 1000,
    }
    if verdict.state is BudgetState.EXCEEDED:
        logger.warning("Budget exceeded before attempt %d: %s", attempt, verdict.reason)
        limits = ctx.budget_limits
        await store.emit_event(
            ctx.task.id,
            None,
            "budget_exceeded",
            {
                "reason": verdict.reason,
                **spent,
                "max_cost_usd": float(limits.max_cost_usd) if limits.max_cost_usd else None,
                "max_wall_seconds": limits.max_wall_seconds,
            },
        )
        ctx.log.note("budget_exceeded", verdict.reason)
        return AttemptResult(Verdict.BUDGET_EXCEEDED, reason=verdict.reason)

    if verdict.state is BudgetState.WARN and not ctx.budget_warned:
        ctx.budget_warned = True
        await store.emit_event(
            ctx.task.id, None, "budget_warning", {"reason": verdict.reason, **spent}
        )
        ctx.log.note("budget_warning", verdict.reason)
        await notify.budget_warning(
            task_key=ctx.task.key,
            repo_name=ctx.repo.name,
            reason=verdict.reason,
            cum_cost=ctx.spend.cost_usd,
            cum_wall_ms=ctx.spend.wall_ms,
            max_cost=ctx.task.max_cost_usd,
            max_wall=ctx.task.max_wall_seconds,
        )
    return None


async def _call_agent(
    ctx: TaskContext, backend: AgentBackend, *, run_id: int
) -> tuple[AgentResult, int]:
    async with async_session() as db:
        notes = await task_notes.take_unread(db, task_id=ctx.task.id)
    if notes:
        ctx.log.note("notes", f"{len(notes)} operator note(s) added to this attempt's prompt")
    prompt = build_prompt(ctx, notes_block=task_notes.render_notes_block(notes))

    started_ms = _now_ms()
    result = await agent_call.run_agent(
        agent_call.AgentInvocation(
            backend=backend,
            worktree_path=ctx.worktree_path,
            prompt=prompt,
            model=ctx.model,
            allowed_tools=ctx.repo.allowed_tools,
            session_id=ctx.session_id,
            timeout_minutes=ctx.repo.timeout_minutes,
            task_id=ctx.task.id,
            run_id=run_id,
            billing_mode=ctx.billing_mode,
            max_turns=ctx.max_turns,
        )
    )
    return result, _now_ms() - started_ms


def _reported_cost(result: AgentResult) -> Decimal:
    return Decimal(str(result.cost_usd or 0))


def _record_spend(ctx: TaskContext, result: AgentResult, duration_ms: int) -> None:
    """Every attempt, failed or not, consumes real wall time and real cost."""
    if ctx.billing_mode != "max":
        ctx.spend.cost_usd += _reported_cost(result)
    ctx.spend.wall_ms += duration_ms


def _log_agent_result(
    ctx: TaskContext, result: AgentResult, *, label: str, duration_ms: int
) -> None:
    raw_cost = float(_reported_cost(result))
    cost_label = (
        f"~${raw_cost:.4f} (Max, not charged)" if ctx.billing_mode == "max" else f"${raw_cost:.4f}"
    )
    rule = "─" * 60
    ctx.log.write(
        f"\n{rule}\n{label} — exit={result.exit_code} turns={result.num_turns} "
        f"duration={duration_ms / 1000:.0f}s cost={cost_label}\n{rule}\n"
    )
    if result.result:
        ctx.log.write(f"RESULT:\n{result.result}\n")
    else:
        ctx.log.write(f"RAW OUTPUT:\n{result.raw_output[:20_000]}\n")


async def _suspended_for_gate(ctx: TaskContext, *, run_id: int) -> bool:
    """True when the agent stopped to ask for a side-effect approval."""
    async with async_session() as db:
        gate = await side_effect_gate.check_open_gate(db, ctx.task.id)
        if gate is None:
            return False
        await side_effect_gate.suspend_for_gate(
            db, task_id=ctx.task.id, run_id=run_id, gate=gate, task_log=ctx.log
        )
    return True


async def _verify_and_deliver(
    ctx: TaskContext, result: AgentResult, *, run_id: int, attempt: int, duration_ms: int
) -> AttemptResult:
    ctx.session_id = result.session_id
    # Subscription billing has no per-token cost; store 0, not the estimate.
    cost_usd = Decimal("0") if ctx.billing_mode == "max" else _reported_cost(result)
    metrics = AttemptMetrics(attempt, result.num_turns, cost_usd, duration_ms)
    logger.info(
        "Agent completed: turns=%d, cost=$%s (billing=%s), duration=%dms",
        result.num_turns,
        _reported_cost(result),
        ctx.billing_mode,
        duration_ms,
    )
    if ctx.git_flow != "untracked":
        await git_ops.ensure_committed(ctx.worktree_path, ctx.task.key, ctx.task.title)
    await store.update_run(
        run_id,
        status="verifying",
        session_id=ctx.session_id,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        turns=result.num_turns,
        claude_output=result.raw_output[:50_000],
    )
    await store.emit_event(
        ctx.task.id,
        run_id,
        "cost_update",
        {"cost_usd": float(cost_usd), "turns": result.num_turns, "duration_ms": duration_ms},
    )

    verify_failure = await _verify(ctx)
    if verify_failure is not None:
        return await verify_failed(ctx, verify_failure, run_id=run_id, attempt=attempt)

    preflight = await run_preflight(ctx, run_id=run_id)
    if preflight.verdict is not PreflightVerdict.PASSED:
        return await preflight_failed(ctx, preflight, run_id=run_id)

    handover = await hand_over(ctx, summary=result.result, metrics=metrics)
    await store.finish_run(run_id, status="success", pr_url=handover.pr_url)
    success = Success(metrics, handover, result.result, preflight.files_changed)
    return AttemptResult(Verdict.SUCCEEDED, success=success)


async def _verify(ctx: TaskContext) -> str | None:
    """Run the repo's build/test/lint commands; return the failure, if any."""
    if ctx.task.verifier_off:
        why = (
            f"{ctx.task.task_type} task runs its own checks"
            if ctx.task.runs_own_checks
            else "skip_verify flag set"
        )
        logger.info("Skipping worker verifier (%s)", why)
        ctx.log.write(f"\n[Worker verification skipped — {why}]\n")
        return None

    await store.set_task_status(ctx.task.id, "verifying")
    started_ms = _now_ms()
    ok, error = await verifier.run_verify(
        worktree_path=ctx.worktree_path,
        pre_cmd=ctx.repo.pre_cmd,
        build_cmd=ctx.repo.build_cmd,
        test_cmd=ctx.repo.test_cmd,
        lint_cmd=ctx.repo.lint_cmd,
        log_file=ctx.log,
    )
    ctx.spend.wall_ms += _now_ms() - started_ms  # verifier time counts too
    return None if ok else error
