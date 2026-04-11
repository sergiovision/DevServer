"""Agent runner — main execution logic for a coding task.

Orchestrates: lock -> worktree -> repo_map -> reality_gate -> memory_recall
           -> (optional plan_gate) -> claude CLI -> verify -> pr_preflight
           -> PR -> notify.
Implements retry loop with session persistence, targeted error classification,
and a per-task cost/wall-clock circuit breaker.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.base import async_session
from models.daily_stat import DailyStat
from models.repo import Repo
from models.task import Task
from models.task_event import TaskEvent
from models.task_run import TaskRun
from services import (
    error_classifier,
    git_ops,
    memory as memory_svc,
    patch_ops,
    plan_gate,
    pr_preflight,
    reality_gate,
    repo_map,
    telegram,
    verifier,
)

# Budget circuit breaker — emit a warning event when cumulative cost or wall
# time crosses this fraction of the task's hard limit, once per task.
_BUDGET_WARN_THRESHOLD = 0.8

# Rate-limit handling for the Anthropic API. When the Claude CLI subprocess
# fails with a 429, we sleep and retry the SAME call without consuming a
# task-level retry attempt — burning a full retry on a transient quota
# error costs another ~5K tokens of context for nothing.
_RATE_LIMIT_PATTERNS = re.compile(
    r"rate_limit_error|rate limit of \d+\s*(?:input\s+)?tokens? per minute|429",
    re.IGNORECASE,
)
# Backoff schedule (seconds) — for the 30K-tokens/minute default Anthropic
# tier, the bucket clears in well under a minute, so 30/60/120 plus jitter
# is plenty before we give up and surface the error.
_RATE_LIMIT_BACKOFF_SCHEDULE = (30, 60, 120)
_RATE_LIMIT_JITTER_SECONDS = 10


def _is_rate_limit_error(stdout: str, stderr: str, exit_code: int) -> bool:
    """True if the Claude CLI run looks like an Anthropic 429.

    Checks both streams because the CLI sometimes prints the API error to
    stdout (as part of its JSON output) and sometimes to stderr (as a raw
    line). Exit code alone is not enough — every Claude failure is non-zero.
    """
    if exit_code == 0:
        return False
    return bool(_RATE_LIMIT_PATTERNS.search(stdout)) or bool(_RATE_LIMIT_PATTERNS.search(stderr))

logger = logging.getLogger(__name__)


async def _acquire_lock(db: AsyncSession, repo_name: str, task_key: str) -> bool:
    """Acquire a repo lock using PostgreSQL. Returns True if lock acquired."""
    # First, clean up any expired locks
    await db.execute(text(
        "DELETE FROM repo_locks WHERE repo_name = :repo_name AND expires_at < NOW()"
    ), {"repo_name": repo_name})
    # Try to insert (fails silently if lock exists via ON CONFLICT DO NOTHING)
    result = await db.execute(text("""
        INSERT INTO repo_locks (repo_name, task_key, acquired_at, expires_at)
        VALUES (:repo_name, :task_key, NOW(), NOW() + interval '1 hour')
        ON CONFLICT (repo_name) DO NOTHING
        RETURNING repo_name
    """), {"repo_name": repo_name, "task_key": task_key})
    await db.commit()
    return result.fetchone() is not None


async def _extend_lock(db: AsyncSession, repo_name: str) -> None:
    """Extend the repo lock expiry by 1 hour."""
    await db.execute(text(
        "UPDATE repo_locks SET expires_at = NOW() + interval '1 hour' WHERE repo_name = :repo_name"
    ), {"repo_name": repo_name})
    await db.commit()


async def _release_lock(db: AsyncSession, repo_name: str, task_key: str) -> None:
    """Release the repo lock if held by this task."""
    await db.execute(text(
        "DELETE FROM repo_locks WHERE repo_name = :repo_name AND task_key = :task_key"
    ), {"repo_name": repo_name, "task_key": task_key})
    await db.commit()


def _check_budget(
    *,
    cum_cost: Decimal,
    cum_wall_ms: int,
    max_cost_usd: Decimal | None,
    max_wall_seconds: int | None,
    claude_mode: str,
) -> tuple[str, str]:
    """Evaluate the budget circuit breaker state.

    Returns ``(state, reason)`` where state is one of:
      - ``"ok"``        — well under budget, no action
      - ``"warn"``      — crossed the warn threshold, emit warning
      - ``"exceeded"``  — over a hard limit, caller must stop the task
    """
    # Cost is always zero in Max mode — skip cost enforcement there.
    if max_cost_usd is not None and claude_mode != "max":
        if cum_cost >= max_cost_usd:
            return "exceeded", f"cost ${cum_cost} exceeded budget ${max_cost_usd}"
        if cum_cost >= max_cost_usd * Decimal(str(_BUDGET_WARN_THRESHOLD)):
            return "warn", f"cost ${cum_cost} at {_BUDGET_WARN_THRESHOLD:.0%} of ${max_cost_usd}"

    if max_wall_seconds is not None:
        cum_wall_s = cum_wall_ms / 1000
        if cum_wall_s >= max_wall_seconds:
            return "exceeded", f"wall-clock {cum_wall_s:.0f}s exceeded budget {max_wall_seconds}s"
        if cum_wall_s >= max_wall_seconds * _BUDGET_WARN_THRESHOLD:
            return "warn", f"wall-clock {cum_wall_s:.0f}s at {_BUDGET_WARN_THRESHOLD:.0%} of {max_wall_seconds}s"

    return "ok", ""


async def _emit_event(
    session: AsyncSession,
    task_id: int,
    run_id: int | None,
    event_type: str,
    payload: dict,
) -> None:
    """Insert a task_event row (triggers PG NOTIFY via database trigger)."""
    event = TaskEvent(
        task_id=task_id,
        run_id=run_id,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    await session.commit()


async def _update_task_status(session: AsyncSession, task_id: int, status: str) -> None:
    await session.execute(
        update(Task).where(Task.id == task_id).values(status=status, updated_at=datetime.now(timezone.utc))
    )
    await session.commit()


def _build_prompt(
    repo_name: str,
    branch_name: str,
    task_key: str,
    title: str,
    description: str,
    acceptance: str,
    error_context: str = "",
    repo_map_text: str = "",
    reality_signal_text: str = "",
    memory_recall_text: str = "",
    approved_plan_text: str = "",
    is_resume: bool = False,
) -> str:
    """Build the user-message prompt for one Claude CLI invocation.

    When ``is_resume`` is True, the Claude CLI is being invoked with
    ``--resume <session_id>`` and the agent already has the full task
    context, repo map, reality signal, memory recall and approved plan in
    its conversation history from the original turn. Re-sending all of
    that on every retry was the dominant cause of the 30K-tokens/minute
    rate-limit failures we hit during Phase 1+2 testing — each retry was
    sending an extra ~5K tokens of static context that the model already
    had. So on resume we return a minimal next-message prompt with only
    the error/remediation block (or a short "continue" message if there
    is no error context, e.g. after a max_turns pause).
    """
    if is_resume:
        # The session already has every Phase 1 context block in its
        # history. Send only the next user message — the smaller the
        # better for rate-limit headroom.
        if error_context:
            return error_context
        return (
            "Continue with the previous task. Resume where you left off "
            "and finish the implementation. Commit your changes when done."
        )

    parts = [
        f"You are an autonomous coding agent working on repository: {repo_name}",
        f"Branch: {branch_name}",
        f"Task: {task_key} - {title}",
        "",
        "## Task Description",
        description or "(no description provided)",
        "",
        "## Acceptance Criteria",
        acceptance or "(none specified)",
    ]

    # Evidence-before-action blocks (Phase 1). Injected only when available so
    # the prompt stays clean for tasks where a block failed to generate.
    if repo_map_text:
        parts.extend(["", repo_map_text])
    if reality_signal_text:
        parts.extend(["", reality_signal_text])
    if memory_recall_text:
        parts.extend(["", memory_recall_text])
    if approved_plan_text:
        parts.extend(["", approved_plan_text])

    parts.extend([
        "",
        "## Instructions",
        "1. Read the CLAUDE.md file first for project context and conventions",
        "2. Understand the existing codebase before making changes",
        "3. Implement the task following existing patterns and conventions",
        "4. Make minimal, focused changes -- only what the task requires",
        "5. Do NOT add extra features, refactoring, or improvements beyond the task",
        f"6. Commit your changes with a clear message referencing {task_key}",
        "7. Ensure all acceptance criteria are met",
    ])

    if error_context:
        parts.extend(["", error_context])

    return "\n".join(parts)


def _render_memory_recall(memories: list[dict]) -> str:
    """Render a list of memory_svc.search_memory hits as a prompt block."""
    if not memories:
        return ""
    lines = [
        "## Prior Experience (from agent_memory)",
        "Summaries of similar past tasks — hints, not ground truth.",
    ]
    # Hard-cap at 3 entries × 200 chars after the Phase 1+2 rate-limit
    # incident; the upstream call site already requests limit=3 but we
    # belt-and-braces here in case a caller passes a longer list.
    for m in memories[:3]:
        mtype = m.get("memory_type", "experience")
        sim = m.get("similarity", 0.0)
        content = (m.get("content") or "").strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"- [{mtype} sim={sim:.2f}] {content}")
    return "\n".join(lines)


async def _run_claude(
    worktree_path: str,
    prompt: str,
    model: str,
    allowed_tools: str,
    session_id: str | None,
    timeout_minutes: int,
    task_id: int,
    run_id: int,
    db: AsyncSession,
    claude_mode: str = "max",
    max_turns: int | None = 100,
) -> dict:
    """Execute Claude Code CLI and stream output.

    Returns dict with keys: result, cost_usd, num_turns, session_id, exit_code.
    claude_mode='max' (default) runs without ANTHROPIC_API_KEY so Claude CLI uses Max subscription.
    claude_mode='api' uses the API key from the environment.

    On Anthropic 429 (rate_limit_error), retries the SAME subprocess call up
    to ``len(_RATE_LIMIT_BACKOFF_SCHEDULE)`` times with backoff. This is
    inside ``_run_claude`` on purpose: a 429 burns no agent progress, so we
    must not consume a task-level retry attempt for it (which would re-build
    the full prompt and double down on the rate-limit problem).
    """
    cmd = [
        settings.claude_bin,
        "-p", prompt,
        "--permission-mode", "auto",
        "--output-format", "json",
        "--model", model,
    ]
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])

    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    if session_id:
        cmd.extend(["--resume", session_id])

    timeout_seconds = timeout_minutes * 60

    # Build subprocess environment
    if claude_mode == "max":
        # Strip API key so Claude CLI falls back to Max subscription (OAuth login)
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        logger.info("Running Claude CLI in %s (model=%s, timeout=%dm, billing=Max)", worktree_path, model, timeout_minutes)
    else:
        env = None  # inherit full environment (includes ANTHROPIC_API_KEY)
        logger.info("Running Claude CLI in %s (model=%s, timeout=%dm, billing=API)", worktree_path, model, timeout_minutes)

    async def _spawn_once() -> tuple[int, str, str]:
        """Spawn the Claude CLI subprocess one time and collect its output.

        Returns (exit_code, stdout_text, stderr_text). Raises on timeout —
        the outer function maps timeouts to a structured failure dict so the
        rate-limit retry loop never sees them.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            stdout_data.decode(errors="replace"),
            stderr_data.decode(errors="replace"),
        )

    # Outer loop: 429-aware retry. We sleep and retry the SAME call on
    # rate_limit; any other failure breaks out immediately and the existing
    # outer-loop error path takes over.
    exit_code = -1
    raw_output = ""
    stderr_text = ""
    rate_limit_attempts = 0

    while True:
        try:
            exit_code, raw_output, stderr_text = await _spawn_once()
        except asyncio.TimeoutError:
            return {
                "result": "",
                "cost_usd": 0,
                "num_turns": 0,
                "session_id": session_id,
                "exit_code": -1,
                "error": f"Claude CLI timed out after {timeout_minutes}m",
            }

        if not _is_rate_limit_error(raw_output, stderr_text, exit_code):
            break

        if rate_limit_attempts >= len(_RATE_LIMIT_BACKOFF_SCHEDULE):
            logger.error(
                "Claude CLI rate-limited %d times in a row, giving up",
                rate_limit_attempts,
            )
            break

        backoff = _RATE_LIMIT_BACKOFF_SCHEDULE[rate_limit_attempts]
        # Add jitter so concurrent workers don't synchronise their retries.
        jitter = random.uniform(0, _RATE_LIMIT_JITTER_SECONDS)
        sleep_for = backoff + jitter
        rate_limit_attempts += 1
        logger.warning(
            "Anthropic 429 (attempt %d/%d) — sleeping %.0fs before retrying",
            rate_limit_attempts, len(_RATE_LIMIT_BACKOFF_SCHEDULE), sleep_for,
        )
        await _emit_event(db, task_id, run_id, "rate_limit_backoff", {
            "attempt": rate_limit_attempts,
            "max_attempts": len(_RATE_LIMIT_BACKOFF_SCHEDULE),
            "sleep_seconds": round(sleep_for, 1),
        })
        await asyncio.sleep(sleep_for)

    # Log stderr lines as task events
    if stderr_text:
        for line in stderr_text.splitlines():
            if line.strip():
                await _emit_event(db, task_id, run_id, "log_line", {"line": line, "stream": "stderr"})

    # Parse JSON output
    result_text = ""
    cost_usd = 0
    num_turns = 0
    new_session_id = session_id

    subtype = ""
    errors: list[str] = []
    try:
        data = json.loads(raw_output)
        result_text = data.get("result", "")
        cost_usd = data.get("cost_usd", 0)
        num_turns = data.get("num_turns", 0)
        new_session_id = data.get("session_id", session_id) or session_id
        subtype = data.get("subtype", "")
        errors = data.get("errors", [])
    except (json.JSONDecodeError, TypeError):
        result_text = raw_output
        logger.warning("Claude output was not valid JSON, using raw text")

    return {
        "result": result_text,
        "cost_usd": cost_usd,
        "num_turns": num_turns,
        "session_id": new_session_id,
        "exit_code": exit_code,
        "raw_output": raw_output,
        "subtype": subtype,
        "errors": errors,
    }


async def _run_plan_gate(
    *,
    db: AsyncSession,
    task_id: int,
    task_key: str,
    title: str,
    description: str,
    acceptance: str,
    repo: Repo,
    worktree_path: str,
    claude_mode: str,
    task_log,
) -> str | None:
    """Run the plan → approve → implement gate for interactive-mode tasks.

    Returns the rendered approved-plan prompt block on success.
    Returns None if the plan was rejected, timed out, or failed to generate —
    in which case the task has already been marked blocked/failed and the
    caller should stop.
    """
    # Clear any prior approval state in case the task is being re-run.
    await plan_gate.reset_approval(db, task_id)

    # Insert a dedicated run row for the plan phase so the dashboard has
    # somewhere to hang the plan_json column.
    plan_run = TaskRun(
        task_id=task_id,
        attempt=0,
        branch=None,
        status="planning",
    )
    db.add(plan_run)
    await db.commit()
    await db.refresh(plan_run)
    plan_run_id = plan_run.id

    task_log.write(f"\n[plan_gate] generating plan for {task_key}...\n")
    task_log.flush()

    # Run Claude in plan-only mode. Cap turns hard so this phase is cheap.
    plan_prompt = plan_gate.build_plan_prompt(
        repo_name=repo.name,
        task_key=task_key,
        title=title,
        description=description,
        acceptance=acceptance,
    )
    plan_result = await _run_claude(
        worktree_path=worktree_path,
        prompt=plan_prompt,
        model=repo.claude_model,
        allowed_tools="Read,Glob,Grep",  # read-only tools — no edits during planning
        session_id=None,
        timeout_minutes=min(repo.timeout_minutes, 15),
        task_id=task_id,
        run_id=plan_run_id,
        db=db,
        claude_mode=claude_mode,
        max_turns=30,
    )

    raw_plan_output = plan_result.get("result") or plan_result.get("raw_output", "")
    plan = plan_gate.parse_plan_output(raw_plan_output)

    await plan_gate.save_plan(db, plan_run_id, plan)

    if plan.get("_parse_error"):
        await _emit_event(db, task_id, plan_run_id, "plan_pending", {
            "plan": plan,
            "parse_error": plan.get("_parse_error"),
        })
        task_log.write(
            f"\n[plan_gate] plan failed to parse: {plan.get('_parse_error')}\n"
        )
        task_log.flush()
    else:
        await _emit_event(db, task_id, plan_run_id, "plan_pending", {
            "plan": plan,
            "files_to_touch": plan.get("files_to_touch", []),
        })
        task_log.write(
            f"\n[plan_gate] plan ready ({len(plan.get('files_to_touch', []))} files, "
            f"{len(plan.get('steps', []))} steps) — awaiting approval\n"
        )
        task_log.flush()

    await telegram.tg_send(
        f"\U0001f4cb *{task_key}* plan ready for review\n"
        f"Summary: {plan.get('summary', '(no summary)')[:200]}\n"
        f"Files: {len(plan.get('files_to_touch', []))}"
    )

    # Mark the plan run as finished (it's not an implementation attempt).
    await db.execute(
        update(TaskRun).where(TaskRun.id == plan_run_id).values(
            status="awaiting_approval",
            finished_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    # Poll for approval.
    verdict, ts = await plan_gate.wait_for_approval(db, task_id)

    if verdict == "approved":
        await _emit_event(db, task_id, plan_run_id, "plan_approved", {"ts": str(ts)})
        return plan_gate.render_plan_for_implementation(plan)

    if verdict == "rejected":
        await _emit_event(db, task_id, plan_run_id, "plan_rejected", {"ts": str(ts)})
        await _update_task_status(db, task_id, "blocked")
        await telegram.tg_send(f"\u26d4 *{task_key}* plan rejected")
        return None

    # timeout
    await _emit_event(db, task_id, plan_run_id, "plan_rejected", {"reason": "timeout"})
    await _update_task_status(db, task_id, "blocked")
    await telegram.tg_send(f"\u23f0 *{task_key}* plan approval timed out")
    return None


async def run_task(task_id: int, claude_mode: str = "max", max_turns: int | None = None) -> bool:
    """Execute a full task lifecycle. Returns True on success."""
    async with async_session() as db:
        # Load task with repo
        task = await db.get(Task, task_id)
        if not task:
            logger.error("Task %d not found", task_id)
            return False

        repo = await db.get(Repo, task.repo_id)
        if not repo:
            logger.error("Repo %d not found for task %d", task.repo_id, task_id)
            return False

        task_key = task.task_key
        title = task.title
        description = task.description or ""
        acceptance = task.acceptance or ""
        skip_verify = bool(task.skip_verify)
        # Per-task model overrides repo default
        effective_model = getattr(task, "claude_model", None) or repo.claude_model
        # Per-task turn budget: job payload → task field → default 50
        # None means unlimited (no --max-turns flag passed to Claude)
        task_max_turns = getattr(task, "max_turns", None)
        effective_max_turns: int | None = max_turns if max_turns is not None else (task_max_turns if task_max_turns is not None else 50)
        # Sentinel -1 from job payload means "unlimited" (long_running preset)
        if effective_max_turns == -1:
            effective_max_turns = None
        # Phase 2 #6 — per-task budget circuit breaker. Both NULL = no limit.
        max_cost_usd: Decimal | None = getattr(task, "max_cost_usd", None)
        max_wall_seconds: int | None = getattr(task, "max_wall_seconds", None)
        repo_name = repo.name
        branch_name = f"agent/{task_key}"

        turns_label = str(effective_max_turns) if effective_max_turns is not None else "unlimited"
        logger.info(
            "=== Starting task: %s - %s (repo: %s, model: %s, max_turns: %s) ===",
            task_key, title, repo_name, effective_model, turns_label,
        )

        # Acquire repo lock
        if not await _acquire_lock(db, repo_name, task_key):
            logger.warning("Could not acquire lock for repo %s", repo_name)
            return False

        success = False
        session_id: str | None = None
        pr_url: str | None = None
        worktree_path: str | None = None

        # Per-task log file: logs/tasks/{task_key}.log
        task_log_path = os.path.join(settings.log_dir, f"{task_key}.log")
        task_log = open(task_log_path, "a", encoding="utf-8")  # noqa: WPS515

        try:
            task_log.write(
                f"\n{'='*60}\n"
                f"Task {task_key} started at {datetime.now(timezone.utc).isoformat()}\n"
                f"{'='*60}\n"
            )
            task_log.flush()

            # Update status
            await _update_task_status(db, task_id, "running")
            await _emit_event(db, task_id, None, "status_change", {"status": "running"})
            await telegram.tg_send(
                f"\U0001f527 Starting: *{task_key}* -- {title} ({repo_name})"
            )

            # Setup worktree
            worktree_path, branch_name = await git_ops.setup_worktree(
                repo_name=repo_name,
                clone_url=repo.clone_url,
                default_branch=repo.default_branch,
                task_key=task_key,
                gitea_token=repo.gitea_token,
            )

            # ─── Pre-execution evidence (Phase 1) ─────────────────────────
            # These blocks are generated once per task, before any Claude run.
            # Each is allowed to fail independently — never let context-gathering
            # crash the task itself.
            repo_map_text = ""
            reality_signal_text = ""
            memory_recall_text = ""
            prior_memories: list[dict] = []
            reality_signal: dict = {}

            try:
                rm_text, rm_stats = repo_map.build_repo_map(worktree_path)
                repo_map_text = rm_text
                await _emit_event(db, task_id, None, "repo_map_built", rm_stats)
                task_log.write(
                    f"\n[repo_map] {rm_stats.get('files', 0)} files, "
                    f"{rm_stats.get('symbols', 0)} symbols, "
                    f"{rm_stats.get('chars', 0)} chars\n"
                )
                task_log.flush()
            except Exception:
                logger.exception("repo_map generation failed for %s", task_key)

            try:
                reality_signal = await reality_gate.run_reality_gate(
                    db=db,
                    repo_id=repo.id,
                    worktree_path=worktree_path,
                    repo_map_text=repo_map_text,
                    task_key=task_key,
                    title=title,
                    description=description,
                    acceptance=acceptance,
                    branch_name=branch_name,
                    gitea_url=repo.gitea_url,
                    gitea_owner=repo.gitea_owner,
                    gitea_repo=repo.gitea_repo,
                    gitea_token=repo.gitea_token,
                )
                reality_signal_text = reality_gate.render_for_prompt(reality_signal)
                await _emit_event(db, task_id, None, "reality_signal", {
                    "score": reality_signal.get("score"),
                    "confidence": reality_signal.get("confidence"),
                    "warnings": reality_signal.get("warnings", []),
                    "degraded_sources": reality_signal.get("degraded_sources", []),
                    "evidence": reality_signal.get("evidence", []),
                })
                task_log.write(
                    f"\n[reality_gate] score={reality_signal.get('score')}"
                    f" confidence={reality_signal.get('confidence')}"
                    f" warnings={len(reality_signal.get('warnings', []))}\n"
                )
                task_log.flush()
            except Exception:
                logger.exception("reality_gate failed for %s", task_key)

            # Memory recall — Tier 2 #5. Query once, inject into the prompt.
            # Limit dropped from 5 → 3 to reduce per-prompt token bloat after
            # the Phase 1+2 rate-limit incident.
            try:
                memory_query = f"{task_key} {title}\n{description}"
                prior_memories = await memory_svc.search_memory(
                    session=db,
                    repo_id=repo.id,
                    query=memory_query,
                    limit=3,
                )
                memory_recall_text = _render_memory_recall(prior_memories)
                if prior_memories:
                    await _emit_event(db, task_id, None, "memory_recall", {
                        "count": len(prior_memories),
                        "top_similarity": prior_memories[0].get("similarity", 0.0),
                    })
                    task_log.write(
                        f"\n[memory_recall] {len(prior_memories)} prior entries, "
                        f"top sim={prior_memories[0].get('similarity', 0.0):.2f}\n"
                    )
                    task_log.flush()
            except Exception:
                logger.exception("memory_recall failed for %s", task_key)

            # ─── Interactive mode: plan → approve → implement ─────────────
            approved_plan_text = ""
            if task.mode == "interactive":
                approved_plan_text = await _run_plan_gate(
                    db=db,
                    task_id=task_id,
                    task_key=task_key,
                    title=title,
                    description=description,
                    acceptance=acceptance,
                    repo=repo,
                    worktree_path=worktree_path,
                    claude_mode=claude_mode,
                    task_log=task_log,
                )
                if approved_plan_text is None:
                    # plan was rejected or timed out — task already marked blocked
                    success = False
                    return success
                task_log.write("\n[plan_gate] plan approved, proceeding to implementation\n")
                task_log.flush()

            error_context = ""
            # Track recurring error classes across retries. Phase 1 #4:
            # if the same class hits twice, escalate instead of burning another full retry.
            error_class_counts: dict[str, int] = {}

            # Phase 2 #6 — budget tracking. Measures only agent-active time
            # (Claude subprocess + verifier), not lock/worktree/plan-gate waits.
            cum_cost = Decimal("0")
            cum_wall_ms = 0
            budget_warned = False
            budget_blocked = False

            # Phase 2 #7 — extract plan allow-list for PR preflight, if this
            # is an interactive task with an approved plan.
            preflight_allowlist: list[str] | None = None
            if task.mode == "interactive" and approved_plan_text:
                # Read the latest plan_json from the task_runs row we just created.
                plan_row = await db.execute(text(
                    "SELECT plan_json FROM task_runs "
                    "WHERE task_id = :tid AND plan_json IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1"
                ), {"tid": task_id})
                row = plan_row.fetchone()
                if row and row[0]:
                    plan_obj = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    files = plan_obj.get("files_to_touch") or []
                    if files:
                        preflight_allowlist = list(files)

            # Retry loop
            for attempt in range(1, repo.max_retries + 1):
                # Phase 2 #6 — check budget before spending another retry.
                state, reason = _check_budget(
                    cum_cost=cum_cost,
                    cum_wall_ms=cum_wall_ms,
                    max_cost_usd=max_cost_usd,
                    max_wall_seconds=max_wall_seconds,
                    claude_mode=claude_mode,
                )
                if state == "exceeded":
                    logger.warning("Budget exceeded before attempt %d: %s", attempt, reason)
                    await _emit_event(db, task_id, None, "budget_exceeded", {
                        "reason": reason,
                        "cum_cost_usd": float(cum_cost),
                        "cum_wall_seconds": cum_wall_ms / 1000,
                        "max_cost_usd": float(max_cost_usd) if max_cost_usd is not None else None,
                        "max_wall_seconds": max_wall_seconds,
                    })
                    task_log.write(f"\n[budget_exceeded] {reason}\n")
                    task_log.flush()
                    budget_blocked = True
                    break
                if state == "warn" and not budget_warned:
                    budget_warned = True
                    await _emit_event(db, task_id, None, "budget_warning", {
                        "reason": reason,
                        "cum_cost_usd": float(cum_cost),
                        "cum_wall_seconds": cum_wall_ms / 1000,
                    })
                    task_log.write(f"\n[budget_warning] {reason}\n")
                    task_log.flush()

                logger.info("--- Attempt %d/%d ---", attempt, repo.max_retries)

                # Insert run record
                run = TaskRun(
                    task_id=task_id,
                    attempt=attempt,
                    branch=branch_name,
                    status="started",
                )
                db.add(run)
                await db.commit()
                await db.refresh(run)
                run_id = run.id

                await _emit_event(db, task_id, run_id, "progress", {
                    "attempt": attempt,
                    "max_retries": repo.max_retries,
                })

                # Build prompt. On the FIRST attempt (and any attempt where
                # there is no live session to resume), include the full
                # Phase 1 context blocks. On a resumed session the agent
                # already has all of that in its conversation history —
                # re-sending it would burn ~5K extra input tokens per turn
                # for nothing and is the dominant cause of 429 rate limits.
                is_resume = session_id is not None
                prompt = _build_prompt(
                    repo_name, branch_name, task_key, title,
                    description, acceptance, error_context,
                    repo_map_text=repo_map_text,
                    reality_signal_text=reality_signal_text,
                    memory_recall_text=memory_recall_text,
                    approved_plan_text=approved_plan_text,
                    is_resume=is_resume,
                )

                # Run Claude
                await _extend_lock(db, repo_name)
                start_ms = time.monotonic_ns() // 1_000_000

                claude_result = await _run_claude(
                    worktree_path=worktree_path,
                    prompt=prompt,
                    model=effective_model,
                    allowed_tools=repo.claude_allowed_tools,
                    session_id=session_id,
                    timeout_minutes=repo.timeout_minutes,
                    task_id=task_id,
                    run_id=run_id,
                    db=db,
                    claude_mode=claude_mode,
                    max_turns=effective_max_turns,
                )

                duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
                exit_code = claude_result["exit_code"]

                # Phase 2 #6 — every attempt (success or failure) consumes
                # real wall time and real API cost. Accumulate both into the
                # per-task counters before any branch handling below.
                attempt_raw_cost = Decimal(str(
                    claude_result.get("total_cost_usd")
                    or claude_result.get("cost_usd")
                    or 0
                ))
                if claude_mode != "max":
                    cum_cost += attempt_raw_cost
                cum_wall_ms += duration_ms

                # Write Claude result to task log file
                raw_output = claude_result.get("raw_output", "")
                result_text = claude_result.get("result", "")
                raw_cost_log = claude_result.get("total_cost_usd") or claude_result.get("cost_usd") or 0
                cost_label = f"~${raw_cost_log:.4f} (Max, not charged)" if claude_mode == "max" else f"${raw_cost_log:.4f}"
                task_log.write(
                    f"\n{'─'*60}\n"
                    f"Attempt {attempt} — exit={exit_code} "
                    f"turns={claude_result.get('num_turns', '?')} "
                    f"duration={duration_ms / 1000:.0f}s "
                    f"cost={cost_label}\n"
                    f"{'─'*60}\n"
                )
                if result_text:
                    task_log.write(f"RESULT:\n{result_text}\n")
                else:
                    # fallback: raw output truncated
                    task_log.write(f"RAW OUTPUT:\n{raw_output[:20_000]}\n")
                task_log.flush()

                if exit_code != 0:
                    subtype = claude_result.get("subtype", "")
                    claude_errors = claude_result.get("errors", [])
                    failure_reason = "; ".join(claude_errors) if claude_errors else f"exit code {exit_code}"
                    if subtype:
                        failure_reason = f"{subtype}: {failure_reason}"

                    if subtype == "error_max_turns":
                        # Claude hit the turn limit but made progress — resume the session
                        resumed_session_id = claude_result.get("session_id")
                        logger.warning(
                            "Claude hit max_turns on attempt %d/%d (turns=%s) — resuming session %s",
                            attempt, repo.max_retries,
                            claude_result.get("num_turns", "?"), resumed_session_id,
                        )
                        task_log.write(
                            f"\n[Attempt {attempt}] PAUSED: max_turns reached"
                            f" (turns={claude_result.get('num_turns', '?')})"
                            f" — will resume session {resumed_session_id}\n"
                        )
                        task_log.flush()
                        session_id = resumed_session_id
                        await db.execute(
                            update(TaskRun).where(TaskRun.id == run_id).values(
                                status="failed",
                                finished_at=datetime.now(timezone.utc),
                                error_log="max_turns reached, resuming",
                                duration_ms=duration_ms,
                            )
                        )
                        await db.commit()
                        # Don't update error_context — let the resumed session continue naturally
                        continue

                    logger.error(
                        "Claude failed on attempt %d/%d: %s",
                        attempt, repo.max_retries, failure_reason,
                    )
                    task_log.write(
                        f"\n[Attempt {attempt}] FAILED: {failure_reason}\n"
                    )
                    task_log.flush()
                    # Classify the failure → targeted hint for next retry.
                    cls = error_classifier.classify(raw_output)
                    error_context = error_classifier.build_remediation_block(cls, raw_output)
                    if cls is not None:
                        error_class_counts[cls.key] = error_class_counts.get(cls.key, 0) + 1
                        await _emit_event(db, task_id, run_id, "error_classified", {
                            "class": cls.key,
                            "severity": cls.severity,
                            "hint": cls.hint,
                            "repeat": error_class_counts[cls.key],
                        })
                    await db.execute(
                        update(TaskRun).where(TaskRun.id == run_id).values(
                            status="failed",
                            finished_at=datetime.now(timezone.utc),
                            error_log=failure_reason[:500],
                            duration_ms=duration_ms,
                        )
                    )
                    await db.commit()
                    # Rate limit — back off and retry without burning an attempt.
                    if cls is not None and cls.key == "api.rate_limit":
                        rl_retries = error_class_counts.get("api.rate_limit", 0)
                        if rl_retries < 4:
                            wait_seconds = 60 * (2 ** (rl_retries - 1))  # 60 / 120 / 240
                            logger.warning(
                                "Rate limit hit (attempt %d, backoff #%d) — sleeping %ds",
                                attempt, rl_retries, wait_seconds,
                            )
                            await _emit_event(db, task_id, run_id, "log_line", {
                                "line": f"[rate_limit] backoff {wait_seconds}s (hit #{rl_retries})",
                                "stream": "stderr",
                            })
                            task_log.write(f"\n[rate_limit] sleeping {wait_seconds}s before retry\n")
                            task_log.flush()
                            await asyncio.sleep(wait_seconds)
                            # Continue to next attempt without treating this as a logic failure.
                            continue
                        # Exhausted backoff retries — escalate.
                        logger.warning("Rate limit: exhausted %d backoff retries, escalating", rl_retries)
                        break
                    # Escalate early if the same error class keeps repeating,
                    # or if we hit a 'hard' error class (no point retrying).
                    if cls is not None and (
                        error_class_counts[cls.key] >= 2 or cls.severity == "hard"
                    ):
                        logger.warning(
                            "Escalating: error class %s repeated %d times (severity=%s)",
                            cls.key, error_class_counts[cls.key], cls.severity,
                        )
                        break
                    continue

                # Update session_id for potential resume
                session_id = claude_result.get("session_id")
                # Max subscription has no per-token cost — Claude CLI still reports a
                # calculated equivalent; store 0 to avoid misleading billing figures.
                raw_cost = Decimal(str(claude_result.get("total_cost_usd") or claude_result.get("cost_usd") or 0))
                cost_usd = Decimal("0") if claude_mode == "max" else raw_cost
                num_turns = claude_result["num_turns"]

                logger.info(
                    "Claude completed: turns=%d, cost=$%s (billing=%s), duration=%dms",
                    num_turns, raw_cost, claude_mode, duration_ms,
                )

                # Ensure committed
                await git_ops.ensure_committed(worktree_path, task_key, title)

                # Update run with metrics
                await db.execute(
                    update(TaskRun).where(TaskRun.id == run_id).values(
                        status="verifying",
                        session_id=session_id,
                        cost_usd=cost_usd,
                        duration_ms=duration_ms,
                        turns=num_turns,
                        claude_output=raw_output[:50000],
                    )
                )
                await db.commit()

                await _emit_event(db, task_id, run_id, "cost_update", {
                    "cost_usd": float(cost_usd),
                    "turns": num_turns,
                    "duration_ms": duration_ms,
                })

                # Verify (unless skip_verify is set for this task)
                if skip_verify:
                    logger.info("Skipping verification (skip_verify=True)")
                    if task_log:
                        task_log.write("\n[Verification skipped — skip_verify flag set]\n")
                        task_log.flush()
                    verify_ok, verify_error = True, ""
                else:
                    await _update_task_status(db, task_id, "verifying")
                    await _extend_lock(db, repo_name)
                    verify_start_ms = time.monotonic_ns() // 1_000_000
                    verify_ok, verify_error = await verifier.run_verify(
                        worktree_path=worktree_path,
                        pre_cmd=repo.pre_cmd,
                        build_cmd=repo.build_cmd,
                        test_cmd=repo.test_cmd,
                        lint_cmd=repo.lint_cmd,
                        log_file=task_log,
                    )
                    # Phase 2 #6 — verifier time counts against the wall budget.
                    cum_wall_ms += (time.monotonic_ns() // 1_000_000) - verify_start_ms

                if verify_ok:
                    # Phase 2 #7 — deterministic PR preflight BEFORE pushing.
                    # Checks: author identity, plan allow-list (interactive
                    # mode), secret leaks, oversize files. Hard failures mark
                    # the task blocked. Scope creep is treated as a verifier-
                    # style retry with a targeted hint.
                    preflight = await pr_preflight.run_preflight(
                        worktree_path=worktree_path,
                        base_branch=repo.default_branch,
                        allowlist=preflight_allowlist,
                    )
                    preflight_summary = pr_preflight.summarise(preflight)
                    if preflight.ok:
                        await _emit_event(db, task_id, run_id, "pr_preflight_pass", preflight_summary)
                        task_log.write(
                            f"\n[pr_preflight] PASS — {preflight_summary.get('files_changed', 0)} files\n"
                        )
                        task_log.flush()
                    else:
                        await _emit_event(db, task_id, run_id, "pr_preflight_fail", preflight_summary)
                        violations_str = ", ".join(
                            f"{v.kind}:{v.path or ''}" for v in preflight.violations[:8]
                        )
                        task_log.write(
                            f"\n[pr_preflight] FAIL — {violations_str}\n"
                        )
                        task_log.flush()

                        if preflight.has_hard_failure:
                            logger.error(
                                "PR preflight hard failure on %s: %s",
                                task_key, violations_str,
                            )
                            await db.execute(
                                update(TaskRun).where(TaskRun.id == run_id).values(
                                    status="failed",
                                    finished_at=datetime.now(timezone.utc),
                                    error_log=f"pr_preflight hard failure: {violations_str[:500]}",
                                )
                            )
                            await _update_task_status(db, task_id, "blocked")
                            await db.commit()
                            await telegram.tg_send(
                                f"\u26d4 *{task_key}* blocked by PR preflight\n"
                                f"Violations: {violations_str[:300]}"
                            )
                            success = False
                            break

                        # Scope creep only — recoverable. Inject a structured
                        # hint and continue the retry loop.
                        error_context = preflight.hint
                        await db.execute(
                            update(TaskRun).where(TaskRun.id == run_id).values(
                                status="failed",
                                finished_at=datetime.now(timezone.utc),
                                error_log=f"pr_preflight scope creep: {violations_str[:500]}",
                            )
                        )
                        await _update_task_status(db, task_id, "running")
                        await db.commit()
                        continue

                    # ── Git flow dispatch ────────────────────────────────────
                    git_flow = getattr(task, "git_flow", "branch") or "branch"
                    verify_note = "Skipped" if skip_verify else "PASSED"

                    if git_flow == "branch":
                        pr_body = (
                            f"## {task_key}: {title}\n\n"
                            f"### Changes\n{claude_result['result'][:2000]}\n\n"
                            f"### Verification\n"
                            f"- Build: {verify_note}\n- Tests: {verify_note}\n"
                            f"{'- Lint: ' + verify_note + chr(10) if repo.lint_cmd else ''}\n"
                            f"### Metrics\n"
                            f"- Attempts: {attempt}/{repo.max_retries}\n"
                            f"- Claude turns: {num_turns}\n"
                            f"- Cost: ${cost_usd}\n"
                            f"- Duration: {duration_ms // 1000}s\n\n"
                            f"---\n*Generated by DevServer autonomous agent*"
                        )
                        pr_url = await git_ops.create_gitea_pr(
                            worktree_path=worktree_path,
                            branch_name=branch_name,
                            default_branch=repo.default_branch,
                            title=f"[{task_key}] {title}",
                            body=pr_body,
                            gitea_url=repo.gitea_url,
                            gitea_owner=repo.gitea_owner,
                            gitea_repo=repo.gitea_repo,
                            gitea_token=repo.gitea_token,
                        )
                        if not pr_url:
                            logger.warning("PR creation failed for %s, branch was pushed", task_key)

                    elif git_flow == "commit":
                        ok = await git_ops.commit_to_default_branch(
                            worktree_path=worktree_path,
                            branch_name=branch_name,
                            default_branch=repo.default_branch,
                            task_key=task_key,
                            title=title,
                        )
                        pr_url = None
                        if not ok:
                            logger.warning("Direct commit failed for %s", task_key)

                    else:  # patch — no push, no PR
                        pr_url = None
                        logger.info("git_flow=patch: skipping push for %s", task_key)

                    await db.execute(
                        update(TaskRun).where(TaskRun.id == run_id).values(
                            status="success",
                            finished_at=datetime.now(timezone.utc),
                            pr_url=pr_url,
                        )
                    )
                    await _update_task_status(db, task_id, "test")
                    await db.commit()

                    # Update daily stats
                    today = datetime.now(timezone.utc).date()
                    stat = await db.get(DailyStat, today)
                    if stat:
                        stat.completed += 1
                        stat.cost_usd += cost_usd
                        stat.total_duration_ms += duration_ms
                        stat.total_turns += num_turns
                    else:
                        db.add(DailyStat(
                            date=today,
                            completed=1,
                            cost_usd=cost_usd,
                            total_duration_ms=duration_ms,
                            total_turns=num_turns,
                        ))
                    await db.commit()

                    git_flow_labels = {
                        "branch": f"PR: {pr_url or 'push failed'}",
                        "commit": "Committed directly to " + repo.default_branch,
                        "patch": "Patch generated (no push)",
                    }
                    await telegram.tg_send(
                        f"\u2705 *{task_key}* done\n"
                        f"{git_flow_labels.get(git_flow, '')}\n"
                        f"Attempts: {attempt} | Turns: {num_turns} | Cost: ${cost_usd}"
                    )

                    # Option A — auto-generate downloadable patches for the
                    # branch so operators can apply the changes to a
                    # production repo by hand via ``git am``. Runs against
                    # the bare repo so the live worktree reset in the
                    # finally block does not affect this step. Entirely
                    # best-effort: a failure is logged but does not demote
                    # the successful task.
                    try:
                        patchset = await patch_ops.generate_patches(
                            task_key=task_key,
                            repo_name=repo_name,
                            base_branch=repo.default_branch,
                            branch_name=branch_name,
                        )
                        await _emit_event(db, task_id, run_id, "patches_generated", {
                            "ok": patchset.ok,
                            "commits": patchset.commits,
                            "files": len(patchset.files),
                            "files_changed": patchset.files_changed,
                            "insertions": patchset.insertions,
                            "deletions": patchset.deletions,
                            "error": patchset.error,
                        })
                        if patchset.ok:
                            task_log.write(
                                f"\n[patches] generated {len(patchset.files)} files "
                                f"({patchset.commits} commits, +{patchset.insertions}/-{patchset.deletions})\n"
                            )
                        else:
                            task_log.write(
                                f"\n[patches] generation failed: {patchset.error}\n"
                            )
                        task_log.flush()
                    except Exception:
                        logger.exception("patch_ops.generate_patches failed for %s", task_key)

                    # Tier 2 #5 (write side): persist this successful run as an
                    # experience memory so future similar tasks benefit from
                    # memory recall. Failures are left out on purpose — we do
                    # not want the agent to learn failed patterns.
                    try:
                        summary = (
                            f"Task {task_key}: {title}\n"
                            f"Description: {(description or '')[:300]}\n"
                            f"Outcome: completed in {attempt} attempt(s), "
                            f"{num_turns} turns, PR: {pr_url or 'none'}\n"
                            f"Result: {(result_text or '')[:600]}"
                        )
                        await memory_svc.store_memory(
                            session=db,
                            repo_id=repo.id,
                            content=summary,
                            memory_type="experience",
                            task_id=task_id,
                            metadata={
                                "task_key": task_key,
                                "attempts": attempt,
                                "turns": num_turns,
                                "pr_url": pr_url,
                                "reality_score": reality_signal.get("score"),
                            },
                        )
                    except Exception:
                        logger.exception("Failed to store success memory for %s", task_key)

                    success = True
                    break
                else:
                    # Verification failed — classify and build a targeted hint.
                    logger.error("Verification failed on attempt %d", attempt)
                    cls = error_classifier.classify(verify_error)
                    error_context = error_classifier.build_remediation_block(cls, verify_error)
                    if cls is not None:
                        error_class_counts[cls.key] = error_class_counts.get(cls.key, 0) + 1
                        await _emit_event(db, task_id, run_id, "error_classified", {
                            "class": cls.key,
                            "severity": cls.severity,
                            "hint": cls.hint,
                            "repeat": error_class_counts[cls.key],
                            "source": "verifier",
                        })
                    await db.execute(
                        update(TaskRun).where(TaskRun.id == run_id).values(
                            status="failed",
                            finished_at=datetime.now(timezone.utc),
                            error_log=f"Verification failed: {verify_error[:5000]}",
                        )
                    )
                    await _update_task_status(db, task_id, "running")
                    await db.commit()
                    await _emit_event(db, task_id, run_id, "error", {
                        "error": verify_error[:2000],
                        "attempt": attempt,
                    })
                    # Escalate early on hard errors or on repeated same class.
                    if cls is not None and (
                        error_class_counts[cls.key] >= 2 or cls.severity == "hard"
                    ):
                        logger.warning(
                            "Escalating: verify error class %s repeated %d times (severity=%s)",
                            cls.key, error_class_counts[cls.key], cls.severity,
                        )
                        break

            # All attempts exhausted
            if not success:
                # Phase 2 #6 — budget-blocked tasks get their own terminal
                # status and a different telegram message so operators can
                # distinguish "failed after N retries" from "ran out of budget".
                if budget_blocked:
                    await _update_task_status(db, task_id, "blocked")
                    today = datetime.now(timezone.utc).date()
                    stat = await db.get(DailyStat, today)
                    if stat:
                        stat.failed += 1
                    else:
                        db.add(DailyStat(date=today, failed=1))
                    await db.commit()

                    await telegram.tg_send(
                        f"\u23f3 *{task_key}* blocked — budget exceeded\n"
                        f"Repo: {repo_name}\n"
                        f"Cost: ${cum_cost} / wall: {cum_wall_ms/1000:.0f}s"
                    )
                else:
                    await _update_task_status(db, task_id, "failed")
                    today = datetime.now(timezone.utc).date()
                    stat = await db.get(DailyStat, today)
                    if stat:
                        stat.failed += 1
                    else:
                        db.add(DailyStat(date=today, failed=1))
                    await db.commit()

                    await telegram.tg_send(
                        f"\u274c *{task_key}* FAILED after {repo.max_retries} attempts\n"
                        f"Repo: {repo_name}\n"
                        f"Error: {error_context[:200]}"
                    )

        except Exception as exc:
            tb = traceback.format_exc()
            logger.exception("Unhandled error in task %s", task_key)
            await _update_task_status(db, task_id, "failed")
            await _emit_event(db, task_id, None, "error", {
                "error": str(exc),
                "traceback": tb[-3000:],
            })
            await telegram.tg_send(
                f"\u274c *{task_key}* crashed\n```\n{str(exc)[:300]}\n```"
            )

        finally:
            task_log.write(
                f"\n{'='*60}\n"
                f"Task {task_key} finished at {datetime.now(timezone.utc).isoformat()} "
                f"(success={success})\n"
                f"{'='*60}\n"
            )
            task_log.close()

            # Preserve any uncommitted work so the next run can resume from it
            if worktree_path and not success:
                try:
                    committed = await git_ops.ensure_committed(
                        worktree_path, task_key, f"WIP: {title}"
                    )
                    if committed:
                        logger.info("Saved uncommitted work as WIP commit on %s", branch_name)
                except Exception:
                    logger.exception("Failed to save WIP commit for task %s", task_key)

            # Always reset worktree to default branch so next task starts clean
            await git_ops.reset_worktree(repo_name, repo.default_branch)
            await _release_lock(db, repo_name, task_key)

    return success
