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
from services import agent_backends, side_effect_gate, skills
from services.decomposer import _domain_hint
from services.notify import notify

logger = logging.getLogger(__name__)

_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash,WebFetch"
_DEFAULT_TIMEOUT_MIN = 30


def _build_skill_prompt(
    *, task_key: str, title: str, description: str, acceptance: str,
    domain_hint: str, workdir: str,
    skill_block: str, gate_block: str,
) -> str:
    parts = [
        f"You are an autonomous agent working a non-coding task. {domain_hint}",
        "",
        f"Working directory: {workdir}",
        "Save every deliverable (drafts, research notes, trackers) as files in "
        "the working directory. This is a non-coding task: there is no code "
        "repo, no build/test, and no pull request.",
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


async def run_skill_task(
    session: AsyncSession,
    *,
    task_id: int,
    claude_mode: str = "max",
    max_turns: int | None = None,
) -> bool:
    """Execute a non-coding (repo-less) task via a Skill. Returns True on success."""
    # Lazy import avoids a circular import at module load (agent_runner imports
    # this module only inside run_task).
    from services.agent_runner import _emit_event, _run_agent, _update_task_status

    row = (await session.execute(
        text(
            """
            SELECT t.task_key, t.title, t.description, t.acceptance,
                   t.agent_vendor, t.claude_model, t.max_turns
            FROM tasks t
            WHERE t.id = :tid
            """
        ),
        {"tid": task_id},
    )).mappings().fetchone()
    if not row:
        logger.error("skill task %d not found", task_id)
        return False

    task_key = row["task_key"]
    domain = "generic"
    backend = agent_backends.get_backend(row["agent_vendor"] or agent_backends.DEFAULT_VENDOR)
    effective_model = row["claude_model"] or ""
    eff_turns = max_turns if max_turns is not None else (row["max_turns"] or 50)
    if eff_turns == -1:
        eff_turns = None

    # Scratch working directory (no git).
    workdir = Path(settings.log_dir) / "skill-work" / task_key
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("could not create skill workdir for %s", task_key)
        return False

    # Build the prompt: task + domain skill + (opt) approval-gate instructions.
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
    )

    # Run row + running status.
    run_id = (await session.execute(
        text("INSERT INTO task_runs (task_id, attempt, status, started_at) "
             "VALUES (:t, 1, 'started', NOW()) RETURNING id"),
        {"t": task_id},
    )).fetchone()[0]
    await session.commit()
    await _update_task_status(session, task_id, "running")

    start = datetime.now(timezone.utc)
    logger.info("=== Starting skill task: %s (domain=%s, model=%s) ===",
                task_key, domain, effective_model or "(default)")

    try:
        result = await _run_agent(
            backend=backend,
            worktree_path=str(workdir),
            prompt=prompt,
            model=effective_model,
            allowed_tools=_ALLOWED_TOOLS,
            session_id=None,
            timeout_minutes=_DEFAULT_TIMEOUT_MIN,
            task_id=task_id,
            run_id=run_id,
            db=session,
            claude_mode=claude_mode,
            max_turns=eff_turns,
            task_key=task_key,
        )
    except Exception as exc:
        logger.exception("skill task %s crashed", task_key)
        await session.execute(
            text("UPDATE task_runs SET status='failed', finished_at=NOW(), error_log=:e WHERE id=:r"),
            {"e": str(exc)[:4000], "r": run_id},
        )
        await _update_task_status(session, task_id, "failed")
        await notify.text(f"FAIL {task_key} (skill) crashed: {str(exc)[:200]}")
        return False

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    exit_code = result.get("exit_code", 0)
    output = result.get("result") or result.get("raw_output", "")
    turns = result.get("num_turns", 0)
    cost = result.get("total_cost_usd") or result.get("cost_usd") or 0

    # Side-effect gate: the agent raised a blocking gate and stopped — suspend.
    open_gate = await side_effect_gate.check_open_gate(session, task_id)
    if open_gate:
        await side_effect_gate.suspend_for_gate(
            session, task_id=task_id, run_id=run_id, gate=open_gate,
        )
        return False

    if exit_code != 0:
        await session.execute(
            text("UPDATE task_runs SET status='failed', finished_at=NOW(), "
                 "duration_ms=:d, turns=:n, claude_output=:o WHERE id=:r"),
            {"d": duration_ms, "n": turns, "o": (output or "")[:200_000], "r": run_id},
        )
        await _update_task_status(session, task_id, "failed")
        await notify.text(f"FAIL {task_key} (skill, {domain}) failed (exit {exit_code})")
        return False

    # Success: persist the result as an artifact + mark done.
    try:
        (workdir / "RESULT.md").write_text(output or "(no output)", encoding="utf-8")
    except Exception:
        logger.exception("could not write RESULT.md for %s", task_key)

    await session.execute(
        text("UPDATE task_runs SET status='success', finished_at=NOW(), "
             "duration_ms=:d, turns=:n, cost_usd=:c, claude_output=:o WHERE id=:r"),
        {"d": duration_ms, "n": turns, "c": cost, "o": (output or "")[:200_000], "r": run_id},
    )
    await _emit_event(session, task_id, run_id, "skill_invoked",
                      {"domain": domain, "workdir": str(workdir), "turns": turns})
    await _update_task_status(session, task_id, "done")
    # Plain text (no Markdown / no raw path) — keeps Telegram from choking on
    # entity parsing for filesystem paths.
    await notify.text(f"OK {task_key} (skill, {domain}) done — artifacts saved")
    logger.info("Skill task %s done (%d turns, %dms)", task_key, turns, duration_ms)
    return True
