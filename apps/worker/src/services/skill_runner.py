"""Runner for conversational tasks: ``skill``, ``research`` and repo-less tasks.

These are single agent runs with no verifier, no retries and no git output.
A task tied to a repo runs inside that repo (the operator's folder, or a
clean worktree) under the repo lock, so it reads the right files; a repo-less
task runs in a scratch directory. The agent's final answer is mirrored to the
Task Log — for a research task it *is* the deliverable — and saved as
``RESULT.md`` next to any other artifacts, outside the repo.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings
from models.base import async_session
from services import agent_backends, git_ops, repo_lock, side_effect_gate, skills, task_notes
from services.agent_backends import AgentResult
from services.decomposer import _domain_hint
from services.notify import notify
from services.runner import agent_call, outcome, store
from services.runner.context import RepoSpec, TaskSpec, load_specs, resolve_max_turns
from services.runner.task_log import TaskLog

logger = logging.getLogger(__name__)

_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash,WebFetch"
# Research reads, thinks and answers; Write stays so it can save long artifacts.
_RESEARCH_TOOLS = "Read,Write,Glob,Grep,WebFetch,WebSearch,Bash"
_TIMEOUT_MINUTES = 30
_CONTINUE_NUDGE = (
    "Please continue this task. Review your previous work, then refine "
    "or extend it as needed. Put your COMPLETE updated answer in your "
    "final message — it is shown to the operator in the Task Log."
)


@dataclass(frozen=True)
class _Workspace:
    workdir: Path
    artifact_dir: Path
    repo_note: str


async def run_lightweight_task(
    *,
    task_id: int,
    task_type: str = "skill",
    claude_mode: str = "max",
    max_turns: int | None = None,
) -> bool:
    """Execute a conversational / repo-less task. Returns True on success.

    Raises :class:`services.repo_lock.RepoLockBusy` when its repo is busy.
    """
    specs = await load_specs(task_id)
    if specs is None:
        return False
    task, repo, _ = specs
    artifact_dir = Path(settings.log_dir) / "skill-work" / task.key
    artifact_dir.mkdir(parents=True, exist_ok=True)
    session_id = await store.consume_continuation(task.id) if task.is_continuation else None
    turns = resolve_max_turns(max_turns, task.max_turns)

    if repo is None:
        workspace = _Workspace(artifact_dir, artifact_dir, repo_note="")
        return await _run(task, workspace, claude_mode, turns, session_id)

    async with repo_lock.hold(repo.name, owner=task.key):
        workspace = await _checkout(task, repo, artifact_dir)
        try:
            return await _run(task, workspace, claude_mode, turns, session_id)
        finally:
            if not repo.is_local:
                await git_ops.reset_worktree(repo.name, repo.default_branch)


async def _checkout(task: TaskSpec, repo: RepoSpec, artifact_dir: Path) -> _Workspace:
    if repo.is_local:
        workdir = Path(git_ops.resolve_local_root(repo.gitea_url))
    else:
        worktree, _branch = await git_ops.setup_worktree(
            repo_name=repo.name,
            clone_url=repo.clone_url,
            default_branch=repo.default_branch,
            task_key=task.key,
            gitea_token=repo.gitea_token,
            provider=repo.provider,
        )
        workdir = Path(worktree)
    note = (
        f"This task belongs to repository '{repo.name}', checked out at the "
        "working directory above. Read and operate within THAT repository "
        "for all context — do not look elsewhere on disk. This is a "
        "read/answer task: do NOT commit, push, or open pull requests. Save "
        f"any scratch artifacts under {artifact_dir} (outside the repo)."
    )
    return _Workspace(workdir, artifact_dir, note)


async def _prompt(task: TaskSpec, workspace: _Workspace, *, resuming: bool) -> str:
    async with async_session() as db:
        notes = task_notes.render_notes_block(await task_notes.take_unread(db, task_id=task.id))
        if resuming:
            # The session already holds the task; send only the follow-up.
            return notes or _CONTINUE_NUDGE
        if task.task_type == "research":
            prompt = _research_prompt(task, workspace)
        else:
            skill_block = await skills.get_skill_body_for_task(db, task.id)
            gate_block = ""
            if await side_effect_gate.is_enabled(db):
                gate_block = "\n".join(side_effect_gate.render_gate_prompt_block())
            prompt = _skill_prompt(task, workspace, skill_block=skill_block, gate_block=gate_block)
    return f"{prompt}\n\n{notes}" if notes else prompt


async def _run(
    task: TaskSpec,
    workspace: _Workspace,
    billing_mode: str,
    max_turns: int | None,
    session_id: str | None,
) -> bool:
    with TaskLog(settings.log_dir, task.key) as log:
        log.banner(f"Task {task.key} ({task.task_type}) started")
        prompt = await _prompt(task, workspace, resuming=session_id is not None)
        run_id = await store.start_run(task.id, attempt=1, branch="")
        await store.set_task_status(task.id, "running")
        logger.info("=== Starting %s task: %s (model=%s) ===", task.task_type, task.key, task.model)
        started = time.monotonic()
        try:
            result = await agent_call.run_agent(
                agent_call.AgentInvocation(
                    backend=agent_backends.get_backend(task.vendor),
                    worktree_path=str(workspace.workdir),
                    prompt=prompt,
                    model=task.model,
                    allowed_tools=_RESEARCH_TOOLS
                    if task.task_type == "research"
                    else _ALLOWED_TOOLS,
                    session_id=session_id,
                    timeout_minutes=_TIMEOUT_MINUTES,
                    task_id=task.id,
                    run_id=run_id,
                    billing_mode=billing_mode,
                    max_turns=max_turns,
                )
            )
        except asyncio.CancelledError:
            await outcome.record_cancelled(task.id)
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        answer = result.result or result.raw_output
        log.write(
            f"\n{'─' * 60}\n{task.task_type} run — exit={result.exit_code} "
            f"turns={result.num_turns} duration={duration_ms / 1000:.0f}s\n{'─' * 60}\n"
        )
        if answer:
            log.write(f"RESULT:\n{answer}\n")
        return await _record(task, workspace, result, run_id=run_id, duration_ms=duration_ms)


async def _record(
    task: TaskSpec, workspace: _Workspace, result: AgentResult, *, run_id: int, duration_ms: int
) -> bool:
    answer = result.result or result.raw_output
    async with async_session() as db:
        gate = await side_effect_gate.check_open_gate(db, task.id)
        if gate is not None:
            await side_effect_gate.suspend_for_gate(db, task_id=task.id, run_id=run_id, gate=gate)
            return False

    succeeded = result.exit_code == 0
    await store.finish_run(
        run_id,
        status="success" if succeeded else "failed",
        duration_ms=duration_ms,
        turns=result.num_turns,
        cost_usd=result.cost_usd or 0,
        claude_output=(answer or "")[:200_000],
        session_id=result.session_id,
    )
    if not succeeded:
        await store.set_task_status(task.id, "failed")
        await notify.text(f"FAIL {task.key} ({task.task_type}) failed (exit {result.exit_code})")
        await notify.task_state_changed(task_key=task.key, final=True)
        return False

    (workspace.artifact_dir / "RESULT.md").write_text(answer or "(no output)", encoding="utf-8")
    await store.emit_event(
        task.id,
        run_id,
        "skill_invoked",
        {
            "domain": task.task_type,
            "task_type": task.task_type,
            "workdir": str(workspace.workdir),
            "turns": result.num_turns,
        },
    )
    # 'test' = done, awaiting review: Reopen / Continue / Retire stay available.
    await store.set_task_status(task.id, "test")
    done = "answer in Task Log" if task.task_type == "research" else "artifacts saved"
    await notify.text(f"OK {task.key} ({task.task_type}) done — {done}")
    await notify.task_state_changed(task_key=task.key, final=True)
    return True


def _research_prompt(task: TaskSpec, workspace: _Workspace) -> str:
    parts = [
        "You are a research assistant. Answer the request below thoroughly and "
        "accurately. Use web search/fetch to gather and verify current "
        "information when useful, and cite sources inline.",
    ]
    if workspace.repo_note:
        parts += ["", workspace.repo_note]
    parts += [
        "",
        f"## Request: {task.key} — {task.title}",
        task.description or "(no prompt provided)",
        "",
        "Put your COMPLETE answer in your final message — it is printed verbatim "
        "to the operator's Task Log. You may also save long supporting material "
        "as files in the working directory, but the final message must stand on "
        "its own as the answer.",
    ]
    return "\n".join(parts)


def _skill_prompt(
    task: TaskSpec, workspace: _Workspace, *, skill_block: str, gate_block: str
) -> str:
    parts = [
        f"You are an autonomous agent working a non-coding task. "
        f"{_domain_hint(task.task_type, None)}",
        "",
        f"Working directory: {workspace.workdir}",
        workspace.repo_note
        or "Save every deliverable (drafts, research notes, trackers) as files "
        "in the working directory. This is a non-coding task: there is no "
        "code repo, no build/test, and no pull request.",
        "",
        f"## Task: {task.key} — {task.title}",
        task.description or "(no description)",
    ]
    if task.acceptance:
        parts += ["", "## Acceptance criteria", task.acceptance]
    if skill_block:
        parts += ["", skill_block]
    if gate_block:
        parts.append(gate_block)
    parts += [
        "",
        "When done, write a short RESULT.md summarising what you produced and where, then stop.",
    ]
    return "\n".join(parts)
