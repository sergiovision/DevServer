"""Non-coding domain executor.

A leaf in a non-coding project is still an LLM-agent run, but it must NOT touch a
git worktree, verifier, or PR. This runner is the lightweight counterpart to
``agent_runner.run_task`` for those tasks:

    - scratch working directory instead of a git worktree
    - the project's domain Skill injected into the prompt
    - the side-effect approval gate enforced (the safety mechanism for money /
      message / publish / clinical / legal / irreversible actions)
    - artifacts saved to the scratch dir; status → done; no push, no PR

It deliberately reuses ``agent_runner._run_agent`` so all the cross-vendor
machinery (subprocess spawn, 429 backoff, task-event emission, and the
``DEVSERVER_WORKER_URL``/``DEVSERVER_TASK_KEY`` env injection that lets the
agent reach the gate + messaging endpoints) is shared, not duplicated.

``run_task`` dispatches here when ``tasks.repo_id IS NULL``. v1 runs a single
attempt (no retry loop); the gate suspend/resume path handles the
human-in-the-loop case.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.repo import Repo
from services import agent_backends, git_ops, side_effect_gate, skills
from services.decomposer import _domain_hint
from services.notify import notify

logger = logging.getLogger(__name__)

_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash,WebFetch"
# Research is a read/think/answer task — give it web search/fetch but it does
# not need to edit a working tree (Write is kept so it can save long artifacts).
_RESEARCH_TOOLS = "Read,Write,Glob,Grep,WebFetch,WebSearch,Bash"
_DEFAULT_TIMEOUT_MIN = 30


async def _operator_request_block(session: AsyncSession, task_key: str) -> str:
    """Render unread operator messages as a follow-up-instruction block.

    Operator messages sent from the dashboard Messages panel are prompt
    requests. When a finished (status ``test``) skill/research task is
    continued, this drains the unread operator messages and injects them into
    the prompt so the agent answers the operator's latest instruction — its
    reply is then mirrored to the Task Log. Best-effort and Pro-gated: inter-task
    messaging only exists in the Pro build, so a missing module is a silent no-op.
    """
    try:
        from services.pro import task_messaging
    except ImportError:
        return ""
    try:
        msgs = await task_messaging.read_inbox(
            session, task_key=task_key, mark_read=True, include_read=False, limit=20,
        )
    except Exception:
        logger.exception("could not read operator inbox for %s", task_key)
        return ""
    bodies = [m["body"].strip() for m in msgs if (m.get("body") or "").strip()]
    if not bodies:
        return ""
    lines = [
        "## Operator request(s)",
        "The operator sent the message(s) below from the dashboard. Treat them "
        "as the primary, most-recent instruction and address them directly. "
        "Your final message is shown to the operator in the Task Log.",
    ]
    lines += [f"{i}. {b}" for i, b in enumerate(bodies, 1)]
    return "\n".join(lines)


def _build_research_prompt(
    *, task_key: str, title: str, description: str, repo_note: str = "",
) -> str:
    """Research task: the Description IS the full prompt. Answer it directly.

    The model's answer is captured and written to the Task Log (and RESULT.md),
    so the agent is told to put its full answer in its final message rather than
    only in scratch files.
    """
    parts = [
        "You are a research assistant. Answer the request below thoroughly and "
        "accurately. Use web search/fetch to gather and verify current "
        "information when useful, and cite sources inline.",
    ]
    if repo_note:
        parts += ["", repo_note]
    parts += [
        "",
        f"## Request: {task_key} — {title}",
        description or "(no prompt provided)",
        "",
        "Put your COMPLETE answer in your final message — it is printed verbatim "
        "to the operator's Task Log. You may also save long supporting material "
        "as files in the working directory, but the final message must stand on "
        "its own as the answer.",
    ]
    return "\n".join(parts)


def _build_skill_prompt(
    *, task_key: str, title: str, description: str, acceptance: str,
    domain_hint: str, workdir: str,
    skill_block: str, gate_block: str, repo_note: str = "",
) -> str:
    parts = [
        f"You are an autonomous agent working a non-coding task. {domain_hint}",
        "",
        f"Working directory: {workdir}",
    ]
    if repo_note:
        parts.append(repo_note)
    else:
        parts.append(
            "Save every deliverable (drafts, research notes, trackers) as files "
            "in the working directory. This is a non-coding task: there is no "
            "code repo, no build/test, and no pull request."
        )
    parts += [
        "",
        f"## Task: {task_key} — {title}",
        description or "(no description)",
    ]
    if acceptance:
        parts += ["", "## Acceptance criteria", acceptance]
    if skill_block:
        parts += ["", skill_block]
    if gate_block:
        parts.append(gate_block)
    parts += [
        "",
        "When done, write a short RESULT.md summarising what you produced and "
        "where, then stop.",
    ]
    return "\n".join(parts)


async def run_lightweight_task(
    session: AsyncSession,
    *,
    task_id: int,
    task_type: str = "skill",
    claude_mode: str = "max",
    max_turns: int | None = None,
) -> bool:
    """Execute a conversational / repo-less task. Returns True on success.

    Handles the ``skill`` and ``research`` task types (and any legacy repo-less
    task). No verifier and no PR/push, but when the task is tied to a repo the
    agent runs INSIDE that repo (local folder, or a clean worktree for remote
    repos) so it has the right files as context; repo-less tasks run in a
    scratch dir. The repo lock is held for the duration so a concurrent coding
    task can't clobber the checkout. The agent's final answer is mirrored to the
    per-task log file so it shows up in the dashboard's **Task Log** panel (the
    primary deliverable for a ``research`` task).
    """
    # Lazy import avoids a circular import at module load (agent_runner imports
    # this module only inside run_task).
    from services.agent_runner import (
        _acquire_lock, _emit_event, _release_lock, _run_agent, _update_task_status,
    )

    task_type = (task_type or "skill").lower()
    is_research = task_type == "research"

    row = (await session.execute(
        text(
            """
            SELECT t.task_key, t.title, t.description, t.acceptance, t.repo_id,
                   t.agent_vendor, t.claude_model, t.max_turns, t.is_continuation
            FROM tasks t
            WHERE t.id = :tid
            """
        ),
        {"tid": task_id},
    )).mappings().fetchone()
    if not row:
        logger.error("%s task %d not found", task_type, task_id)
        return False

    task_key = row["task_key"]

    # Continuation (Reopen/Continue button or a Messages-panel follow-up): resume
    # the prior agent session instead of starting over, so a skill/research task
    # truly continues the conversation rather than re-answering from scratch.
    # ``run_task`` returns early for light tasks (before its own is_continuation
    # reset), so we both consume and clear the flag here.
    is_continuation = bool(row["is_continuation"])
    continuation_session_id: str | None = None
    if is_continuation:
        sid_row = (await session.execute(
            text(
                "SELECT session_id FROM task_runs "
                "WHERE task_id = :tid AND session_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"tid": task_id},
        )).fetchone()
        continuation_session_id = sid_row[0] if sid_row else None
        await session.execute(
            text("UPDATE tasks SET is_continuation = FALSE WHERE id = :tid"),
            {"tid": task_id},
        )
    domain = task_type
    backend = agent_backends.get_backend(row["agent_vendor"] or agent_backends.DEFAULT_VENDOR)
    effective_model = row["claude_model"] or ""
    eff_turns = max_turns if max_turns is not None else (row["max_turns"] or 50)
    if eff_turns == -1:
        eff_turns = None

    # Per-task log file, shared with the heavy runner so the dashboard's
    # Task Log panel (which tails logs/{task_key}.log) shows this run too.
    task_log_path = Path(settings.log_dir) / f"{task_key}.log"

    def _log(text_blob: str) -> None:
        try:
            with open(task_log_path, "a", encoding="utf-8") as fh:
                fh.write(text_blob)
        except Exception:
            logger.exception("could not write task log for %s", task_key)

    _log(
        f"\n{'='*60}\n"
        f"Task {task_key} ({task_type}) started at "
        f"{datetime.now(timezone.utc).isoformat()}\n"
        f"{'='*60}\n"
    )

    # Scratch dir for artifacts (RESULT.md, scratch notes) — kept OUT of the
    # repo so a research/skill run never pollutes the operator's checkout.
    artifact_dir = Path(settings.log_dir) / "skill-work" / task_key
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("could not create %s artifact dir for %s", task_type, task_key)
        return False

    # Resolve the WORKING DIRECTORY. A skill/research task tied to a repo must
    # run inside that repo so the agent has the right files as context — not in
    # the DevServer project or an empty scratch folder. Local repos run in the
    # operator's folder; remote repos get a clean worktree checkout. We take the
    # repo lock so a concurrent coding task on the same repo can't clobber us,
    # and we NEVER commit/push/PR (these are read/answer tasks).
    repo_id = row["repo_id"]
    repo = await session.get(Repo, repo_id) if repo_id else None
    workdir = artifact_dir
    repo_note = ""
    repo_name: str | None = None
    is_local = False
    lock_held = False

    if repo is not None:
        repo_name = repo.name
        is_local = git_ops.is_local_provider(repo.provider)
        if not await _acquire_lock(session, repo_name, task_key):
            logger.warning("could not acquire lock for repo %s — will retry", repo_name)
            _log(f"\n[blocked] repo {repo_name} is locked by another task; retry later\n")
            return False
        lock_held = True
        try:
            if is_local:
                workdir = Path(git_ops.resolve_local_root(repo.gitea_url))
            else:
                wt, _branch = await git_ops.setup_worktree(
                    repo_name=repo.name,
                    clone_url=repo.clone_url,
                    default_branch=repo.default_branch,
                    task_key=task_key,
                    gitea_token=repo.gitea_token,
                    provider=repo.provider,
                )
                workdir = Path(wt)
        except Exception:
            logger.exception("could not set up repo workdir for %s", task_key)
            _log(f"\n[error] could not prepare repo {repo_name} working directory\n")
            await _release_lock(session, repo_name, task_key)
            return False
        repo_note = (
            f"This task belongs to repository '{repo_name}', checked out at the "
            "working directory above. Read and operate within THAT repository "
            "for all context — do not look elsewhere on disk. This is a "
            "read/answer task: do NOT commit, push, or open pull requests. Save "
            f"any scratch artifacts under {artifact_dir} (outside the repo)."
        )

    # Build the prompt. Research: the Description is the full prompt. Skill:
    # task + domain skill + (opt) approval-gate instructions.
    allowed_tools = _RESEARCH_TOOLS if is_research else _ALLOWED_TOOLS
    if is_research:
        prompt = _build_research_prompt(
            task_key=task_key, title=row["title"],
            description=row["description"] or "", repo_note=repo_note,
        )
    else:
        skill_block = ""
        try:
            skill_block = await skills.get_skill_body_for_task(session, task_id)
        except Exception:
            logger.exception("skill block load failed for %s", task_key)
        gate_block = ""
        try:
            if await side_effect_gate.is_enabled(session):
                gate_block = "\n".join(side_effect_gate.render_gate_prompt_block())
        except Exception:
            logger.exception("gate block failed for %s", task_key)

        prompt = _build_skill_prompt(
            task_key=task_key, title=row["title"], description=row["description"] or "",
            acceptance=row["acceptance"] or "",
            domain_hint=_domain_hint(domain, None),
            workdir=str(workdir), skill_block=skill_block, gate_block=gate_block,
            repo_note=repo_note,
        )

    # Operator follow-up prompts (Messages panel) take precedence — drain unread
    # operator messages and append them so a continued task answers them.
    operator_block = await _operator_request_block(session, task_key)
    if is_continuation and continuation_session_id:
        # Resuming the prior session: the agent already has the full task in its
        # conversation history, so re-sending the whole prompt would duplicate
        # it. Send only the operator's follow-up, or a short nudge when the
        # operator just clicked Continue/Reopen without a new instruction.
        prompt = operator_block or (
            "Please continue this task. Review your previous work, then refine "
            "or extend it as needed. Put your COMPLETE updated answer in your "
            "final message — it is shown to the operator in the Task Log."
        )
    elif operator_block:
        prompt = f"{prompt}\n\n{operator_block}"

    async def _cleanup() -> None:
        """Release the repo lock and reset a remote worktree (best-effort)."""
        if repo is None or not lock_held:
            return
        if not is_local:
            try:
                await git_ops.reset_worktree(repo_name, repo.default_branch)
            except Exception:
                logger.exception("worktree reset failed for %s", repo_name)
        try:
            await _release_lock(session, repo_name, task_key)
        except Exception:
            logger.exception("lock release failed for %s", repo_name)

    # Run row + running status.
    run_id = (await session.execute(
        text("INSERT INTO task_runs (task_id, attempt, status, started_at) "
             "VALUES (:t, 1, 'started', NOW()) RETURNING id"),
        {"t": task_id},
    )).fetchone()[0]
    await session.commit()
    await _update_task_status(session, task_id, "running")

    start = datetime.now(timezone.utc)
    logger.info("=== Starting %s task: %s (model=%s) ===",
                task_type, task_key, effective_model or "(default)")

    try:
        result = await _run_agent(
            backend=backend,
            worktree_path=str(workdir),
            prompt=prompt,
            model=effective_model,
            allowed_tools=allowed_tools,
            session_id=continuation_session_id,
            timeout_minutes=_DEFAULT_TIMEOUT_MIN,
            task_id=task_id,
            run_id=run_id,
            db=session,
            claude_mode=claude_mode,
            max_turns=eff_turns,
            task_key=task_key,
        )
    except Exception as exc:
        logger.exception("%s task %s crashed", task_type, task_key)
        _log(f"\n[crashed] {str(exc)[:2000]}\n")
        await session.execute(
            text("UPDATE task_runs SET status='failed', finished_at=NOW(), error_log=:e WHERE id=:r"),
            {"e": str(exc)[:4000], "r": run_id},
        )
        await _update_task_status(session, task_id, "failed")
        await notify.text(f"FAIL {task_key} ({task_type}) crashed: {str(exc)[:200]}")
        await _cleanup()
        return False

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    exit_code = result.get("exit_code", 0)
    output = result.get("result") or result.get("raw_output", "")
    turns = result.get("num_turns", 0)
    cost = result.get("total_cost_usd") or result.get("cost_usd") or 0
    # Persist the session id so a later Reopen/Continue can resume this run.
    new_session_id = result.get("session_id")

    # Mirror the run summary + the model's answer into the Task Log so the
    # dashboard shows it (the answer is the deliverable for research tasks).
    _log(
        f"\n{'─'*60}\n"
        f"{task_type} run — exit={exit_code} turns={turns} "
        f"duration={duration_ms / 1000:.0f}s\n"
        f"{'─'*60}\n"
    )
    if output:
        _log(f"RESULT:\n{output}\n")

    # Side-effect gate: the agent raised a blocking gate and stopped — suspend.
    open_gate = await side_effect_gate.check_open_gate(session, task_id)
    if open_gate:
        await side_effect_gate.suspend_for_gate(
            session, task_id=task_id, run_id=run_id, gate=open_gate,
        )
        await _cleanup()
        return False

    if exit_code != 0:
        await session.execute(
            text("UPDATE task_runs SET status='failed', finished_at=NOW(), "
                 "duration_ms=:d, turns=:n, claude_output=:o, session_id=:sid "
                 "WHERE id=:r"),
            {"d": duration_ms, "n": turns, "o": (output or "")[:200_000],
             "sid": new_session_id, "r": run_id},
        )
        await _update_task_status(session, task_id, "failed")
        await notify.text(f"FAIL {task_key} ({task_type}) failed (exit {exit_code})")
        await _cleanup()
        return False

    # Success: persist the result as an artifact + mark done. RESULT.md goes to
    # the scratch artifact dir (never into the repo working tree).
    try:
        (artifact_dir / "RESULT.md").write_text(output or "(no output)", encoding="utf-8")
    except Exception:
        logger.exception("could not write RESULT.md for %s", task_key)

    await session.execute(
        text("UPDATE task_runs SET status='success', finished_at=NOW(), "
             "duration_ms=:d, turns=:n, cost_usd=:c, claude_output=:o, "
             "session_id=:sid WHERE id=:r"),
        {"d": duration_ms, "n": turns, "c": cost, "o": (output or "")[:200_000],
         "sid": new_session_id, "r": run_id},
    )
    # 'skill_invoked' is the existing event type; reuse it for both light
    # types and tag the actual task_type in the payload.
    await _emit_event(session, task_id, run_id, "skill_invoked",
                      {"domain": domain, "task_type": task_type,
                       "workdir": str(workdir), "turns": turns})
    # Land in 'test' (post-success "awaiting review") — the same terminal state
    # coding/script tasks reach — so Reopen / Continue / Retire are available and
    # the operator can drop a follow-up prompt and continue the same task.
    await _update_task_status(session, task_id, "test")
    # Plain text (no Markdown / no raw path) — keeps Telegram from choking on
    # entity parsing for filesystem paths.
    _done_msg = (
        f"OK {task_key} (research) done — answer in Task Log"
        if is_research else
        f"OK {task_key} ({task_type}) done — artifacts saved"
    )
    await notify.text(_done_msg)
    logger.info("%s task %s done (%d turns, %dms)", task_type, task_key, turns, duration_ms)
    await _cleanup()
    return True


# Backward-compatible alias — earlier code called this entry point
# ``run_skill_task``; it now dispatches through the generalised runner.
async def run_skill_task(
    session: AsyncSession,
    *,
    task_id: int,
    claude_mode: str = "max",
    max_turns: int | None = None,
) -> bool:
    return await run_lightweight_task(
        session, task_id=task_id, task_type="skill",
        claude_mode=claude_mode, max_turns=max_turns,
    )
