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
    agent_backends,
    compaction,
    error_classifier,
    git_ops,
    repo_map,
    telegram,
    verifier,
)
from services.agent_backends import AgentBackend
from services.notify import notify

# Pro features: if the services/pro/ folder exists, load real implementations.
# If it's absent (public MIT repo), fall back to no-op stubs so the free
# version compiles and runs without errors.
try:
    from services.pro import hooks as pro
    _HAS_PRO = True
except ImportError:
    from services._free_hooks import FreeHooks
    pro = FreeHooks()
    _HAS_PRO = False


# Rate-limit handling applies to every vendor — when the agent CLI
# subprocess fails with a 429, we sleep and retry the SAME call without
# consuming a task-level retry attempt. Burning a full retry on a transient
# quota error costs another ~5K tokens of context for nothing.
#
# Each vendor detects its OWN 429 shape via ``AgentBackend.is_rate_limit_error``.
# This module only knows the generic backoff schedule.
_RATE_LIMIT_BACKOFF_SCHEDULE = (30, 60, 120)
_RATE_LIMIT_JITTER_SECONDS = 10

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
    compacted_context: str = "",
    is_resume: bool = False,
    skip_verify: bool = False,
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
    # NOTE: ``compacted_context`` is an alternative to the evidence stack —
    # when set, it REPLACES repo_map/memory_recall/reality_signal because
    # the summariser has already distilled prior attempts into what matters.
    if compacted_context:
        parts.extend(["", compacted_context])
    else:
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

    if skip_verify:
        parts.extend([
            "",
            "## Verification policy — SKIPPED",
            "The operator has set `skip_verify=true` for this task. Do NOT run",
            "the project's test suite, build, or lint commands. Do NOT spawn",
            "`npm test`, `pytest`, `cargo test`, `go test`, `dotnet test`,",
            "`npm run build`, `tsc`, `eslint`, `ruff`, or equivalent. Read and",
            "edit code freely, but skip the verify step — the operator will",
            "run checks manually after you finish. Proceed straight from the",
            "implementation to the commit.",
        ])

    # Inter-task messaging prompt block — Pro-only. When the pro package
    # is absent the /internal/tasks/.../messages/* endpoints do not exist,
    # so teaching the agent to curl them would just cause confused 404s.
    if _HAS_PRO:
        parts.extend([
            "",
            "## Inter-task messaging (optional)",
            "You can coordinate with other concurrently-running tasks or hand off",
            "questions to the human operator via the DevServer messaging bus.",
            "The subprocess env exposes $DEVSERVER_WORKER_URL and $DEVSERVER_TASK_KEY.",
            "",
            "- List live peer tasks:",
            "    curl -s \"$DEVSERVER_WORKER_URL/internal/sessions/list\"",
            "- Read your own inbox (drains unread by default):",
            "    curl -s \"$DEVSERVER_WORKER_URL/internal/tasks/$DEVSERVER_TASK_KEY/messages/inbox\"",
            "- Send a message to another task or to 'operator' (the human):",
            "    curl -s -X POST \\",
            "      \"$DEVSERVER_WORKER_URL/internal/tasks/$DEVSERVER_TASK_KEY/messages/send\" \\",
            "      -H 'content-type: application/json' \\",
            "      -d '{\"to_task_key\":\"operator\",\"kind\":\"note\",\"body\":\"...\"}'",
            "",
            "IMPORTANT: the operator can send you messages mid-run. Check your",
            "inbox at the start of the task and again before any major commit or",
            "irreversible step. If the operator's message contradicts or amends",
            "the task description, follow the message — it is the most recent",
            "human intent. Do NOT poll in a tight loop; once per major step is",
            "enough.",
            "",
            "ALWAYS REPLY to operator messages — silent execution is a bug.",
            "When you drain an operator message from your inbox:",
            "1. Send a brief acknowledgement reply (to_task_key='operator', kind='response').",
            "   • For a request/note: confirm receipt and state what you're about to do.",
            "   • For a question (e.g. 'how are you doing?', 'is X done?'): answer it directly.",
            "   • One or two sentences is plenty — no wall of text.",
            "2. Then act on any actionable content.",
            "3. Send a final 'done' reply when you have committed the requested change.",
            "",
            "Use send_message to peer tasks sparingly — only for blocking questions,",
            "cross-task handoffs, or status updates another task is waiting on.",
        ])

    if error_context:
        parts.extend(["", error_context])

    return "\n".join(parts)


    # _render_memory_recall moved to services/pro/__init__.py (ProHooks.render_memory_recall)


async def _run_agent(
    backend: AgentBackend,
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
    task_key: str | None = None,
) -> dict:
    """Execute a vendor-agnostic coding-agent CLI and collect its output.

    Delegates everything vendor-specific to ``backend`` (command
    construction, environment, rate-limit detection, output parsing) and
    keeps the shared machinery here: subprocess spawning, per-call timeout,
    429-aware retry with backoff, and task-event emission.

    Returns the legacy dict shape that the existing retry loop consumes:
    ``result``, ``cost_usd``, ``num_turns``, ``session_id``, ``exit_code``,
    ``raw_output``, ``subtype``, ``errors``.

    On a vendor-specific 429 (detected via ``backend.is_rate_limit_error``),
    retries the SAME subprocess call up to ``len(_RATE_LIMIT_BACKOFF_SCHEDULE)``
    times with jittered backoff. A 429 burns no agent progress, so we must
    not consume a task-level retry attempt for it.

    The ``claude_mode`` parameter stays named that way for backwards
    compatibility with the job payload — it's passed through as the
    ``billing_mode`` argument to the backend's ``build_env`` and only the
    Claude backend does anything meaningful with it.
    """
    cmd = backend.build_command(
        prompt=prompt,
        model=model,
        allowed_tools=allowed_tools,
        session_id=session_id,
        max_turns=max_turns,
    )
    env = backend.build_env(billing_mode=claude_mode)

    # Inject inter-task messaging env vars (Pro only). Agents can curl
    # the worker via these two variables to read their inbox, list peers,
    # or message other tasks mid-run. The endpoints they point at live
    # in ``routes/pro_internal.py`` — if pro is stripped there is nothing
    # to target, so we skip the injection entirely.
    if task_key and _HAS_PRO:
        if env is None:
            env = dict(os.environ)
        env.setdefault(
            "DEVSERVER_WORKER_URL",
            f"http://{settings.worker_host if settings.worker_host != '0.0.0.0' else '127.0.0.1'}:{settings.worker_port}",
        )
        env["DEVSERVER_TASK_KEY"] = task_key

    if backend.vendor == "google":
        gemini_dir = os.path.join(worktree_path, ".gemini")
        os.makedirs(gemini_dir, exist_ok=True)
        settings_path = os.path.join(gemini_dir, "settings.json")
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"model": {"maxSessionTurns": max_turns if max_turns is not None else -1}}, f)
        except Exception as e:
            logger.warning("Failed to write .gemini/settings.json: %s", e)

    # OpenAI / Azure OpenAI — propagate the custom endpoint overrides so
    # Codex CLI targets Azure AI Foundry (or any OpenAI-compatible proxy)
    # instead of api.openai.com. pydantic-settings reads these from .env
    # into the Settings object but does NOT push them back into
    # os.environ, so we forward them explicitly here.
    if backend.vendor == "openai":
        if settings.openai_base_url or settings.openai_api_version:
            if env is None:
                env = dict(os.environ)
            if settings.openai_base_url:
                env["OPENAI_BASE_URL"] = settings.openai_base_url
            if settings.openai_api_version:
                env["OPENAI_API_VERSION"] = settings.openai_api_version
                # Azure's OpenAI-compatible SDKs also read AZURE_OPENAI_*.
                env.setdefault("AZURE_OPENAI_API_VERSION", settings.openai_api_version)
            if settings.openai_api_key:
                # Azure Codex deployments read AZURE_OPENAI_API_KEY when
                # the endpoint is an *.openai.azure.com URL. Mirror the
                # existing OPENAI_API_KEY across both names so either
                # client path works.
                env.setdefault("AZURE_OPENAI_API_KEY", settings.openai_api_key)
            if settings.openai_base_url:
                env.setdefault("AZURE_OPENAI_ENDPOINT", settings.openai_base_url)

    timeout_seconds = timeout_minutes * 60

    logger.info(
        "Running %s CLI in %s (model=%s, timeout=%dm, billing=%s)",
        backend.label, worktree_path, model, timeout_minutes, claude_mode,
    )

    async def _spawn_once() -> tuple[int, str, str]:
        """Spawn the agent CLI subprocess one time and collect its output.

        Returns (exit_code, stdout_text, stderr_text). Raises on timeout —
        the outer function maps timeouts to a structured failure dict so the
        rate-limit retry loop never sees them.
        """
        # stdin=DEVNULL is required for true headless operation. Without it
        # asyncio inherits the worker's stdin — when the worker runs in a
        # terminal (dev mode) the agent CLI inherits the TTY and may either
        # block on a read or, in Gemini's case, merge stray TTY bytes into
        # the prompt (per `gemini --help`: "Appended to input on stdin if any").
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
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
                "error": f"{backend.label} CLI timed out after {timeout_minutes}m",
            }

        if not backend.is_rate_limit_error(raw_output, stderr_text, exit_code):
            break

        if rate_limit_attempts >= len(_RATE_LIMIT_BACKOFF_SCHEDULE):
            logger.error(
                "%s CLI rate-limited %d times in a row, giving up",
                backend.label, rate_limit_attempts,
            )
            break

        backoff = _RATE_LIMIT_BACKOFF_SCHEDULE[rate_limit_attempts]
        # Add jitter so concurrent workers don't synchronise their retries.
        jitter = random.uniform(0, _RATE_LIMIT_JITTER_SECONDS)
        sleep_for = backoff + jitter
        rate_limit_attempts += 1
        logger.warning(
            "%s 429 (attempt %d/%d) — sleeping %.0fs before retrying",
            backend.label, rate_limit_attempts,
            len(_RATE_LIMIT_BACKOFF_SCHEDULE), sleep_for,
        )
        await _emit_event(db, task_id, run_id, "rate_limit_backoff", {
            "vendor": backend.vendor,
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

    # Parse vendor-specific JSON output into the normalised result shape.
    result = backend.parse_output(raw_output, session_id)
    result.exit_code = exit_code
    return result.to_dict()


    # _run_plan_gate moved to services/pro/__init__.py (ProHooks.run_plan_gate)


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
        is_continuation = bool(getattr(task, "is_continuation", False))
        # Context compaction — when a prior attempt called ``/compact`` (or the
        # auto-compaction branch below fired on a previous run), this column
        # holds a distilled summary of everything tried so far. Its presence
        # causes _build_prompt to skip the Phase-1 evidence stack and inject
        # the summary instead. See services/compaction.py.
        compacted_context: str = getattr(task, "compacted_context", None) or ""
        backup_model = getattr(task, "backup_model", None)
        backup_vendor = getattr(task, "backup_vendor", None)
        # Resolve the agent backend for this task. Defaults to Anthropic
        # when the column is missing or unknown (backwards compatible with
        # every task created before migration 006).
        agent_vendor = getattr(task, "agent_vendor", None) or agent_backends.DEFAULT_VENDOR
        backend = agent_backends.get_backend(agent_vendor)
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

        # Continuation: load session_id from the most recent run so the
        # agent can resume its conversation, and clear the flag immediately.
        continuation_session_id: str | None = None
        if is_continuation:
            last_run_row = await db.execute(text(
                "SELECT session_id FROM task_runs "
                "WHERE task_id = :tid AND session_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ), {"tid": task_id})
            row = last_run_row.fetchone()
            continuation_session_id = row[0] if row else None
            await db.execute(
                update(Task).where(Task.id == task_id).values(is_continuation=False)
            )
            await db.commit()
            logger.info(
                "Continuation mode: session_id=%s", continuation_session_id,
            )

        turns_label = str(effective_max_turns) if effective_max_turns is not None else "unlimited"
        logger.info(
            "=== Starting task: %s - %s (repo: %s, model: %s, max_turns: %s%s) ===",
            task_key, title, repo_name, effective_model, turns_label,
            ", continuation" if is_continuation else "",
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
            await notify.task_start(
                task_key=task_key, title=title, repo_name=repo_name,
                mode=task.mode, vendor=agent_vendor,
                model=effective_model or "",
            )

            # Setup worktree
            worktree_path, branch_name = await git_ops.setup_worktree(
                repo_name=repo_name,
                clone_url=repo.clone_url,
                default_branch=repo.default_branch,
                task_key=task_key,
                gitea_token=repo.gitea_token,
                continuation=is_continuation,
            )

            # ─── Pre-execution evidence (Phase 1) ─────────────────────────
            # These blocks are generated once per task, before any Claude run.
            # Each is allowed to fail independently — never let context-gathering
            # crash the task itself.
            # On continuation, skip Phase 1 entirely — the agent already has
            # context from the previous session and we use --resume.
            repo_map_text = ""
            reality_signal_text = ""
            memory_recall_text = ""
            prior_memories: list[dict] = []
            reality_signal: dict = {}

            # Seed session_id from the continuation session so the first
            # attempt in the retry loop uses --resume.
            if is_continuation and continuation_session_id:
                session_id = continuation_session_id
                task_log.write(
                    f"\n[continuation] resuming session {continuation_session_id}\n"
                )
                task_log.flush()

            if is_continuation:
                task_log.write("\n[continuation] skipping Phase 1 evidence pipeline\n")
                task_log.flush()
            else:
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
                reality_signal = reality_signal, reality_signal_text_raw = await pro.run_reality_gate(
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
                reality_signal_text = reality_signal_text_raw
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
              try:
                memory_query = f"{task_key} {title}\n{description}"
                prior_memories = await pro.search_memory(
                    session=db,
                    repo_id=repo.id,
                    query=memory_query,
                    limit=3,
                )
                memory_recall_text = pro.render_memory_recall(prior_memories)
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
            if not is_continuation and task.mode == "interactive":
                approved_plan_text = await pro.run_plan_gate(
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
                    backend=backend,
                    model=effective_model,
                    # Pass helper functions so ProHooks can call them
                    _run_agent=_run_agent,
                    _emit_event=_emit_event,
                    _update_task_status=_update_task_status,
                    telegram=telegram,
                    TaskRun=TaskRun,
                )
                if approved_plan_text is None:
                    # plan was rejected or timed out — task already marked blocked
                    success = False
                    return success
                task_log.write("\n[plan_gate] plan approved, proceeding to implementation\n")
                task_log.flush()

            error_context = ""
            # Continuation nudge — on a human-initiated /continue, the resume
            # prompt would otherwise be a bare "Continue with the previous
            # task." That's fine when resuming a crashed run, but a common
            # reason operators hit Continue is because the task is in ``test``
            # or ``failed`` and they've just dropped a follow-up message into
            # the task inbox. Seed error_context so the resume prompt points
            # the agent at its inbox before it does anything else.
            if is_continuation and continuation_session_id:
                if _HAS_PRO:
                    error_context = (
                        "## Continuation — human-initiated\n"
                        "The operator has resumed this task. They may have "
                        "dropped a new instruction into your inbox. FIRST, drain "
                        "your inbox before doing anything else:\n"
                        "    curl -s \"$DEVSERVER_WORKER_URL/internal/tasks/"
                        "$DEVSERVER_TASK_KEY/messages/inbox\"\n"
                        "\n"
                        "If you find an operator message:\n"
                        "1. REPLY FIRST — send a brief acknowledgement to "
                        "to_task_key='operator' (kind='response'). For a "
                        "question, answer it directly; for a request, confirm "
                        "what you're about to do. Silent execution is a bug.\n"
                        "2. Then act. Treat the message as the most recent "
                        "human intent — it overrides any previous plan. "
                        "Implement the new scope on the same branch and commit.\n"
                        "3. Send a final 'done' reply when the change is "
                        "committed, then finish.\n"
                        "\n"
                        "If the inbox is empty, resume where you left off."
                    )
                else:
                    error_context = (
                        "## Continuation — human-initiated\n"
                        "The operator has resumed this task. Resume where "
                        "you left off and finish the implementation. Commit "
                        "your changes when done."
                    )
            # Track recurring error classes across retries. Phase 1 #4:
            # if the same class hits twice, escalate instead of burning another full retry.
            error_class_counts: dict[str, int] = {}

            # Phase 2 #6 — budget tracking. Measures only agent-active time
            # (Claude subprocess + verifier), not lock/worktree/plan-gate waits.
            cum_cost = Decimal("0")
            cum_wall_ms = 0
            budget_warned = False
            budget_blocked = False
            budget_reason = ""

            # Phase 2 #7 — extract plan allow-list for PR preflight, if this
            # is an interactive task with an approved plan.
            preflight_allowlist = await pro.get_preflight_allowlist(
                db=db, task_id=task_id, task=task, approved_plan_text=approved_plan_text,
            )

            # Auto-compaction threshold: number of total attempts (primary
            # + backup) after which we transparently summarise the transcript
            # and reset the session to keep the vendor's context window from
            # blowing up. 0 disables the automatic branch entirely — manual
            # /internal/tasks/<key>/compact calls still work.
            compact_after_attempts = 3
            auto_compacted_once = bool(compacted_context)

            # Retry loop
            for attempt in range(1, repo.max_retries + 1):
                # Auto-compaction check. Triggers at most once per task: if
                # we've already consumed ``compact_after_attempts`` attempts
                # and haven't compacted yet, summarise now and drop the
                # session_id so the next attempt starts fresh with the
                # summary as its only context.
                if (
                    not auto_compacted_once
                    and compact_after_attempts > 0
                    and attempt > compact_after_attempts
                ):
                    task_log.write(
                        f"\n[auto-compact] attempt {attempt} > threshold "
                        f"{compact_after_attempts}; summarising transcript\n"
                    )
                    task_log.flush()
                    try:
                        cres = await compaction.compact_task(
                            db, task_id=task_id, reason="auto_after_retries",
                        )
                        if cres["ok"]:
                            compacted_context = cres["summary"]
                            session_id = None  # fresh start — summary IS the history
                            auto_compacted_once = True
                            task_log.write(
                                f"[auto-compact] OK — {cres['chars_in']} → {cres['chars_out']} chars\n"
                            )
                            task_log.flush()
                        else:
                            # Failure is logged by compact_task; keep going.
                            auto_compacted_once = True  # don't retry on every attempt
                    except Exception:
                        logger.exception("auto-compaction failed for %s", task_key)
                        auto_compacted_once = True

                # Phase 2 #6 — check budget before spending another retry.
                state, reason = pro.check_budget(
                    cum_cost=cum_cost,
                    cum_wall_ms=cum_wall_ms,
                    max_cost_usd=max_cost_usd,
                    max_wall_seconds=max_wall_seconds,
                    claude_mode=claude_mode,
                )
                if state == "exceeded":
                    budget_reason = reason
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
                    await notify.budget_warning(
                        task_key=task_key, repo_name=repo_name, reason=reason,
                        cum_cost=cum_cost, cum_wall_ms=cum_wall_ms,
                        max_cost=max_cost_usd, max_wall=max_wall_seconds,
                    )

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
                    compacted_context=compaction.build_compacted_prompt_block(compacted_context),
                    is_resume=is_resume,
                    skip_verify=skip_verify,
                )

                # Run the agent via the resolved backend. The variable is
                # still named ``claude_result`` for continuity with the
                # downstream code that reads ``.get("result")`` etc, but
                # the actual backend can be any of Claude / Gemini / OpenAI
                # / Qwen as determined by ``task.agent_vendor``.
                await _extend_lock(db, repo_name)
                start_ms = time.monotonic_ns() // 1_000_000

                claude_result = await _run_agent(
                    backend=backend,
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
                    task_key=task_key,
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
                    
                    if backend.vendor == "google" and exit_code == 53:
                        subtype = "error_max_turns"
                        
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
                    preflight = await pro.run_preflight(
                        worktree_path=worktree_path,
                        base_branch=repo.default_branch,
                        allowlist=preflight_allowlist,
                    )
                    preflight_summary = pro.summarise_preflight(preflight)
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
                            await notify.preflight_blocked(
                                task_key=task_key,
                                violations=[{"kind": v.kind, "detail": v.detail, "severity": v.severity}
                                            for v in getattr(preflight, "violations", [])],
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

                    commit_ok = True
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
                        commit_ok = await git_ops.commit_to_default_branch(
                            worktree_path=worktree_path,
                            branch_name=branch_name,
                            default_branch=repo.default_branch,
                            task_key=task_key,
                            title=title,
                        )
                        pr_url = None
                        if not commit_ok:
                            logger.warning("Direct commit failed for %s", task_key)

                    else:  # patch — no push, no PR
                        commit_ok = True  # patch is always "ok"
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

                    commit_label = (
                        f"Committed directly to {repo.default_branch}"
                        if commit_ok
                        else f"Direct commit to {repo.default_branch} FAILED"
                    )
                    git_flow_labels = {
                        "branch": f"PR: {pr_url or 'push failed'}",
                        "commit": commit_label,
                        "patch": "Patch generated (no push)",
                    }
                    await notify.task_success(
                        task_key=task_key, git_flow=git_flow, pr_url=pr_url,
                        attempts=attempt, turns=num_turns, cost=cost_usd,
                        duration_ms=duration_ms, repo_name=repo_name,
                    )

                    # Option A — auto-generate downloadable patches for the
                    # branch so operators can apply the changes to a
                    # production repo by hand via ``git am``. Runs against
                    # the bare repo so the live worktree reset in the
                    # finally block does not affect this step. Entirely
                    # best-effort: a failure is logged but does not demote
                    # the successful task.
                    try:
                        patchset = await pro.generate_patches(
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
                        logger.exception("pro.generate_patches failed for %s", task_key)

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
                        await pro.store_memory(
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

            # ── Auto-fallback to backup vendor/model ─────────────────
            # If primary model exhausted retries and a backup is configured,
            # switch vendor+model and run another full retry loop. When
            # backup_vendor differs from the primary, this is a cross-vendor
            # failover (e.g. Anthropic → GLM).
            effective_backup_vendor = backup_vendor or agent_vendor
            has_vendor_switch = effective_backup_vendor != agent_vendor
            has_model_switch = backup_model and backup_model != effective_model
            if (
                not success
                and not budget_blocked
                and (has_vendor_switch or has_model_switch)
            ):
                from_label = f"{agent_vendor}/{effective_model}"
                to_label = f"{effective_backup_vendor}/{backup_model or effective_model}"
                logger.info(
                    "Primary %s exhausted — failing over to %s",
                    from_label, to_label,
                )
                task_log.write(
                    f"\n{'='*60}\n"
                    f"[vendor_failover] switching {from_label} → {to_label}\n"
                    f"{'='*60}\n"
                )
                task_log.flush()

                if has_vendor_switch:
                    backend = agent_backends.get_backend(effective_backup_vendor)
                    await _emit_event(db, task_id, None, "vendor_failover", {
                        "from_vendor": agent_vendor,
                        "from_model": effective_model,
                        "to_vendor": effective_backup_vendor,
                        "to_model": backup_model or effective_model,
                    })
                    await notify.vendor_failover(
                        task_key=task_key,
                        repo_name=repo_name,
                        from_vendor=agent_vendor,
                        from_model=effective_model,
                        to_vendor=effective_backup_vendor,
                        to_model=backup_model or effective_model,
                    )
                else:
                    await _emit_event(db, task_id, None, "backup_model_switch", {
                        "from_model": effective_model,
                        "to_model": backup_model,
                    })
                    await notify.text(
                        f"\U0001f504 *{task_key}* primary model failed — switching to backup: {backup_model}"
                    )

                # Session cannot be resumed across vendors — reset it.
                if has_vendor_switch:
                    session_id = None

                effective_model = backup_model or effective_model
                error_class_counts.clear()
                # Ensure any uncommitted work is preserved before backup run
                if worktree_path:
                    await git_ops.ensure_committed(worktree_path, task_key, f"WIP: {title}")

                for attempt in range(1, repo.max_retries + 1):
                    # Budget check
                    state, reason = pro.check_budget(
                        cum_cost=cum_cost,
                        cum_wall_ms=cum_wall_ms,
                        max_cost_usd=max_cost_usd,
                        max_wall_seconds=max_wall_seconds,
                        claude_mode=claude_mode,
                    )
                    if state == "exceeded":
                        budget_reason = reason
                        logger.warning("Budget exceeded before backup attempt %d: %s", attempt, reason)
                        await _emit_event(db, task_id, None, "budget_exceeded", {
                            "reason": reason,
                            "cum_cost_usd": float(cum_cost),
                            "cum_wall_seconds": cum_wall_ms / 1000,
                        })
                        budget_blocked = True
                        break

                    logger.info("--- Backup attempt %d/%d (%s) ---", attempt, repo.max_retries, effective_model)

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
                        "backup_model": True,
                    })

                    is_resume = session_id is not None
                    prompt = _build_prompt(
                        repo_name, branch_name, task_key, title,
                        description, acceptance, error_context,
                        repo_map_text=repo_map_text,
                        reality_signal_text=reality_signal_text,
                        memory_recall_text=memory_recall_text,
                        approved_plan_text=approved_plan_text,
                        is_resume=is_resume,
                        skip_verify=skip_verify,
                    )

                    await _extend_lock(db, repo_name)
                    start_ms = time.monotonic_ns() // 1_000_000

                    claude_result = await _run_agent(
                        backend=backend,
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
                        task_key=task_key,
                    )

                    duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
                    exit_code = claude_result["exit_code"]

                    attempt_raw_cost = Decimal(str(
                        claude_result.get("total_cost_usd")
                        or claude_result.get("cost_usd")
                        or 0
                    ))
                    if claude_mode != "max":
                        cum_cost += attempt_raw_cost
                    cum_wall_ms += duration_ms

                    raw_output = claude_result.get("raw_output", "")
                    result_text = claude_result.get("result", "")
                    raw_cost_log = claude_result.get("total_cost_usd") or claude_result.get("cost_usd") or 0
                    cost_label = f"~${raw_cost_log:.4f} (Max, not charged)" if claude_mode == "max" else f"${raw_cost_log:.4f}"
                    task_log.write(
                        f"\n{'─'*60}\n"
                        f"Backup attempt {attempt} — exit={exit_code} "
                        f"turns={claude_result.get('num_turns', '?')} "
                        f"duration={duration_ms / 1000:.0f}s "
                        f"cost={cost_label}\n"
                        f"{'─'*60}\n"
                    )
                    if result_text:
                        task_log.write(f"RESULT:\n{result_text}\n")
                    else:
                        task_log.write(f"RAW OUTPUT:\n{raw_output[:20_000]}\n")
                    task_log.flush()

                    if exit_code != 0:
                        subtype = claude_result.get("subtype", "")
                        if backend.vendor == "google" and exit_code == 53:
                            subtype = "error_max_turns"
                        claude_errors = claude_result.get("errors", [])
                        failure_reason = "; ".join(claude_errors) if claude_errors else f"exit code {exit_code}"
                        if subtype:
                            failure_reason = f"{subtype}: {failure_reason}"

                        if subtype == "error_max_turns":
                            session_id = claude_result.get("session_id")
                            await db.execute(
                                update(TaskRun).where(TaskRun.id == run_id).values(
                                    status="failed",
                                    finished_at=datetime.now(timezone.utc),
                                    error_log="max_turns reached, resuming",
                                    duration_ms=duration_ms,
                                )
                            )
                            await db.commit()
                            continue

                        cls = error_classifier.classify(raw_output)
                        error_context = error_classifier.build_remediation_block(cls, raw_output)
                        if cls is not None:
                            error_class_counts[cls.key] = error_class_counts.get(cls.key, 0) + 1
                        await db.execute(
                            update(TaskRun).where(TaskRun.id == run_id).values(
                                status="failed",
                                finished_at=datetime.now(timezone.utc),
                                error_log=failure_reason[:500],
                                duration_ms=duration_ms,
                            )
                        )
                        await db.commit()
                        if cls is not None and (
                            error_class_counts[cls.key] >= 2 or cls.severity == "hard"
                        ):
                            break
                        continue

                    # Success path — identical to primary loop
                    session_id = claude_result.get("session_id")
                    raw_cost = Decimal(str(claude_result.get("total_cost_usd") or claude_result.get("cost_usd") or 0))
                    cost_usd = Decimal("0") if claude_mode == "max" else raw_cost
                    num_turns = claude_result["num_turns"]

                    await git_ops.ensure_committed(worktree_path, task_key, title)

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

                    if skip_verify:
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
                        cum_wall_ms += (time.monotonic_ns() // 1_000_000) - verify_start_ms

                    if verify_ok:
                        preflight = await pro.run_preflight(
                            worktree_path=worktree_path,
                            base_branch=repo.default_branch,
                            allowlist=preflight_allowlist,
                        )
                        if not preflight.ok and preflight.has_hard_failure:
                            await _update_task_status(db, task_id, "blocked")
                            await db.commit()
                            break

                        if not preflight.ok:
                            error_context = preflight.hint
                            await db.execute(
                                update(TaskRun).where(TaskRun.id == run_id).values(
                                    status="failed",
                                    finished_at=datetime.now(timezone.utc),
                                    error_log="pr_preflight scope creep",
                                )
                            )
                            await db.commit()
                            continue

                        git_flow = getattr(task, "git_flow", "branch") or "branch"
                        verify_note = "Skipped" if skip_verify else "PASSED"

                        if git_flow == "branch":
                            failover_label = f"{effective_backup_vendor}/{effective_model}" if has_vendor_switch else effective_model
                            pr_body = (
                                f"## {task_key}: {title}\n\n"
                                f"### Changes\n{claude_result['result'][:2000]}\n\n"
                                f"### Verification\n- Build: {verify_note}\n- Tests: {verify_note}\n"
                                f"### Metrics\n"
                                f"- Failover: {failover_label}\n"
                                f"- Attempts: {attempt}\n"
                                f"- Claude turns: {num_turns}\n"
                                f"- Cost: ${cost_usd}\n"
                                f"- Duration: {duration_ms // 1000}s\n\n"
                                f"---\n*Generated by DevServer autonomous agent (failover: {failover_label})*"
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
                        elif git_flow == "commit":
                            await git_ops.commit_to_default_branch(
                                worktree_path=worktree_path,
                                branch_name=branch_name,
                                default_branch=repo.default_branch,
                                task_key=task_key,
                                title=title,
                            )
                            pr_url = None
                        else:
                            pr_url = None

                        await db.execute(
                            update(TaskRun).where(TaskRun.id == run_id).values(
                                status="success",
                                finished_at=datetime.now(timezone.utc),
                                pr_url=pr_url,
                            )
                        )
                        await _update_task_status(db, task_id, "test")
                        await db.commit()

                        today = datetime.now(timezone.utc).date()
                        stat = await db.get(DailyStat, today)
                        if stat:
                            stat.completed += 1
                            stat.cost_usd += cost_usd
                            stat.total_duration_ms += duration_ms
                            stat.total_turns += num_turns
                        else:
                            db.add(DailyStat(
                                date=today, completed=1, cost_usd=cost_usd,
                                total_duration_ms=duration_ms, total_turns=num_turns,
                            ))
                        await db.commit()

                        await notify.task_success(
                            task_key=task_key, git_flow=git_flow, pr_url=pr_url,
                            attempts=attempt, turns=num_turns, cost=cost_usd,
                            duration_ms=duration_ms, repo_name=repo_name,
                        )

                        try:
                            patchset = await pro.generate_patches(
                                task_key=task_key, repo_name=repo_name,
                                base_branch=repo.default_branch, branch_name=branch_name,
                            )
                            await _emit_event(db, task_id, run_id, "patches_generated", {
                                "ok": patchset.ok, "commits": patchset.commits,
                                "files": len(patchset.files),
                            })
                        except Exception:
                            logger.exception("patch_ops failed for %s (backup)", task_key)

                        try:
                            summary = (
                                f"Task {task_key}: {title}\n"
                                f"Outcome: completed by backup model {effective_model}\n"
                                f"Result: {(result_text or '')[:600]}"
                            )
                            await pro.store_memory(
                                session=db, repo_id=repo.id, content=summary,
                                memory_type="experience", task_id=task_id,
                                metadata={"task_key": task_key, "backup_model": effective_model},
                            )
                        except Exception:
                            logger.exception("Failed to store memory for %s (backup)", task_key)

                        success = True
                        break
                    else:
                        cls = error_classifier.classify(verify_error)
                        error_context = error_classifier.build_remediation_block(cls, verify_error)
                        if cls is not None:
                            error_class_counts[cls.key] = error_class_counts.get(cls.key, 0) + 1
                        await db.execute(
                            update(TaskRun).where(TaskRun.id == run_id).values(
                                status="failed",
                                finished_at=datetime.now(timezone.utc),
                                error_log=f"Verification failed: {verify_error[:5000]}",
                            )
                        )
                        await db.commit()
                        if cls is not None and (
                            error_class_counts[cls.key] >= 2 or cls.severity == "hard"
                        ):
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

                    await notify.budget_exceeded(
                        task_key=task_key, repo_name=repo_name,
                        reason=budget_reason, cum_cost=cum_cost, cum_wall_ms=cum_wall_ms,
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

                    await notify.task_failed(
                        task_key=task_key, repo_name=repo_name,
                        error_context=error_context, attempts=repo.max_retries,
                        cost=cum_cost,
                    )

        except Exception as exc:
            tb = traceback.format_exc()
            logger.exception("Unhandled error in task %s", task_key)
            await _update_task_status(db, task_id, "failed")
            await _emit_event(db, task_id, None, "error", {
                "error": str(exc),
                "traceback": tb[-3000:],
            })
            await notify.text(
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
