"""The phases of a coding-task run, in the order the orchestrator calls them.

1. :func:`prepare_workspace` — mark the task running, check out the code.
2. :func:`gather_evidence`   — repo map, reality signal, memory recall, the
   optional plan gate; may stop the run before any agent attempt.
3. :func:`run_all_attempts`  — the primary model's attempts, then the backup
   model's, through one shared :func:`run_attempts`.
4. :func:`finalise`          — save unfinished work and tidy the checkout.
"""

from __future__ import annotations

import logging

from models.base import async_session
from services import (
    agent_backends,
    compaction,
    git_ops,
    plan_gate,
    side_effect_gate,
    skills,
    telemetry,
)
from services.agent_backends import AgentBackend
from services.notify import notify

from . import evidence, store
from .attempt import run_attempt
from .context import TaskContext
from .results import AttemptResult, Verdict

logger = logging.getLogger(__name__)

#: After this many attempts with one model the transcript is summarised and
#: the next attempt starts a fresh session from the summary.
COMPACT_AFTER_ATTEMPTS = 3

_CONTINUATION_NUDGE = (
    "## Continuation — human-initiated\n"
    "The operator has resumed this task. Resume where "
    "you left off and finish the implementation. Commit "
    "your changes when done."
)


async def prepare_workspace(ctx: TaskContext) -> None:
    task, repo = ctx.task, ctx.repo
    await store.set_task_status(task.id, "running")
    # The OTel root span must open before the first event so events attach.
    telemetry.start_task_span(
        task.id, task.key, vendor=task.vendor, model=ctx.model, mode=task.mode, repo_name=repo.name
    )
    await store.emit_event(task.id, None, "status_change", {"status": "running"})
    await notify.task_start(
        task_key=task.key,
        title=task.title,
        repo_name=repo.name,
        mode=task.mode,
        vendor=task.vendor,
        model=ctx.model,
    )

    if repo.is_local:
        # Local repos run inside the operator's own folder; never a copy.
        local_root = git_ops.resolve_local_root(repo.gitea_url)
        ctx.local_original_branch = await git_ops.get_current_branch(local_root)
        ctx.worktree_path, ctx.branch_name = await git_ops.setup_local_repo(
            local_root=repo.gitea_url,
            task_key=task.key,
            git_flow=ctx.git_flow,
            continuation=task.is_continuation,
        )
    else:
        ctx.worktree_path, ctx.branch_name = await git_ops.setup_worktree(
            repo_name=repo.name,
            clone_url=repo.clone_url,
            default_branch=repo.default_branch,
            task_key=task.key,
            gitea_token=repo.gitea_token,
            provider=repo.provider,
            continuation=task.is_continuation,
        )


async def gather_evidence(ctx: TaskContext) -> bool:
    """Collect the prompt's context blocks. False means: stop, task is blocked."""
    task = ctx.task
    if task.is_continuation:
        ctx.log.note("continuation", "skipping Phase 1 evidence pipeline")
        if ctx.session_id:
            ctx.error_context = _CONTINUATION_NUDGE
    else:
        await evidence.build_repo_map(ctx)
        await evidence.run_reality_gate(ctx)
        if await evidence.abstains(ctx):
            return False
        await evidence.recall_memory(ctx)

    async with async_session() as db:
        if await side_effect_gate.is_enabled(db):
            ctx.gate_prompt = "\n".join(side_effect_gate.render_gate_prompt_block())
        ctx.skill_prompt = await skills.get_skill_body_for_task(db, task.id)

    if task.mode == "interactive" and not task.is_continuation:
        plan = await plan_gate.await_approved_plan(ctx)
        if plan is None:
            return False
        ctx.approved_plan_text = plan.render_for_prompt()
        ctx.preflight_allowlist = list(plan.files_to_touch) or None
        ctx.log.note("plan_gate", "plan approved, proceeding to implementation")
    return True


async def run_attempts(ctx: TaskContext, backend: AgentBackend) -> AttemptResult:
    """Up to ``max_retries`` attempts with the current model."""
    result = AttemptResult(Verdict.RETRY)
    for attempt in range(1, ctx.repo.max_retries + 1):
        if attempt > COMPACT_AFTER_ATTEMPTS and not ctx.auto_compacted:
            await _compact_transcript(ctx, attempt=attempt)
        result = await run_attempt(
            ctx, backend, attempt=attempt, is_backup=ctx.failover_label is not None
        )
        if result.verdict is not Verdict.RETRY:
            return result
    return result


async def run_all_attempts(ctx: TaskContext) -> AttemptResult:
    """The primary model's attempts, then the backup model's if they all fail."""
    result = await run_attempts(ctx, agent_backends.get_backend(ctx.task.vendor))
    if result.verdict not in (Verdict.RETRY, Verdict.ESCALATE):
        return result
    backup = await _switch_to_backup(ctx)
    if backup is None:
        return result
    return await run_attempts(ctx, backup)


async def _compact_transcript(ctx: TaskContext, *, attempt: int) -> None:
    """Summarise earlier attempts so the context window stays bounded. Once."""
    ctx.auto_compacted = True
    ctx.log.write(
        f"\n[auto-compact] attempt {attempt} > threshold {COMPACT_AFTER_ATTEMPTS}; "
        "summarising transcript\n"
    )
    async with async_session() as db:
        result = await compaction.compact_task(db, task_id=ctx.task.id, reason="auto_after_retries")
    if result["ok"]:
        ctx.compacted_context = result["summary"]
        ctx.session_id = None  # the summary is now the whole history
        ctx.log.write(f"[auto-compact] OK — {result['chars_in']} → {result['chars_out']} chars\n")


async def _switch_to_backup(ctx: TaskContext) -> AgentBackend | None:
    """Fail over to the backup vendor/model; None when none is configured."""
    task = ctx.task
    to_vendor = task.backup_vendor or task.vendor
    to_model = task.backup_model or ctx.model
    vendor_switch = to_vendor != task.vendor
    if not vendor_switch and to_model == ctx.model:
        return None

    from_label, to_label = f"{task.vendor}/{ctx.model}", f"{to_vendor}/{to_model}"
    logger.info("Primary %s exhausted — failing over to %s", from_label, to_label)
    ctx.log.banner(f"[vendor_failover] switching {from_label} → {to_label}")
    if vendor_switch:
        await store.emit_event(
            task.id,
            None,
            "vendor_failover",
            {
                "from_vendor": task.vendor,
                "from_model": ctx.model,
                "to_vendor": to_vendor,
                "to_model": to_model,
            },
        )
        await notify.vendor_failover(
            task_key=task.key,
            repo_name=ctx.repo.name,
            from_vendor=task.vendor,
            from_model=ctx.model,
            to_vendor=to_vendor,
            to_model=to_model,
        )
        ctx.session_id = None  # sessions do not carry across vendors
    else:
        await store.emit_event(
            task.id, None, "backup_model_switch", {"from_model": ctx.model, "to_model": to_model}
        )
        await notify.text(f"🔄 *{task.key}* primary model failed — switching to backup: {to_model}")

    ctx.failover_label = to_label if vendor_switch else to_model
    ctx.model = to_model
    ctx.error_class_counts.clear()
    if ctx.git_flow != "untracked":
        await git_ops.ensure_committed(ctx.worktree_path, task.key, f"WIP: {task.title}")
    return agent_backends.get_backend(to_vendor)


async def finalise(ctx: TaskContext, *, success: bool) -> None:
    """Save unfinished work and leave the checkout ready for the next task."""
    telemetry.end_task_span(ctx.task.id, success=success)
    ctx.log.finished(ctx.task.key, success=success)
    if not ctx.worktree_path:
        return  # failed before the checkout existed

    # A WIP commit lets a continuation resume from the agent's work.
    if (
        not success
        and ctx.git_flow != "untracked"
        and await git_ops.ensure_committed(
            ctx.worktree_path, ctx.task.key, f"WIP: {ctx.task.title}"
        )
    ):
        logger.info("Saved uncommitted work as WIP commit on %s", ctx.branch_name)

    if ctx.repo.is_local:
        # The operator's own checkout: never reset; just restore their branch.
        if ctx.git_flow != "untracked":
            await git_ops.restore_local_branch(ctx.repo.gitea_url, ctx.local_original_branch)
    else:
        await git_ops.reset_worktree(ctx.repo.name, ctx.repo.default_branch)
