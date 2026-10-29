"""Evidence gathered before the first attempt ("evidence before action").

Each source is optional: a repo map that fails to build or a reality gate
that errors leaves its prompt block empty, it never fails the task. The one
exception is the opt-in abstain gate, which stops a task whose reality score
says the request does not match the code base.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from models.base import async_session
from services import app_settings, memory, reality_gate, repo_map
from services.notify import notify

from . import store
from .context import TaskContext

logger = logging.getLogger(__name__)

#: Memories pushed into the prompt by default; 0 turns recall off.
DEFAULT_PUSHED_MEMORIES = 2


async def build_repo_map(ctx: TaskContext) -> None:
    try:
        text, stats = repo_map.build_repo_map(ctx.worktree_path)
    except OSError, ValueError:
        logger.exception("repo_map generation failed for %s", ctx.task.key)
        return
    ctx.evidence.repo_map_text = text
    await store.emit_event(ctx.task.id, None, "repo_map_built", stats)
    ctx.log.note(
        "repo_map",
        f"{stats.get('files', 0)} files, {stats.get('symbols', 0)} symbols, "
        f"{stats.get('chars', 0)} chars",
    )


async def run_reality_gate(ctx: TaskContext) -> None:
    task = ctx.task
    try:
        signal = await reality_gate.run_reality_gate(
            worktree_path=ctx.worktree_path,
            repo_map_text=ctx.evidence.repo_map_text,
            title=task.title,
            description=task.description,
            acceptance=task.acceptance,
        )
    except OSError, RuntimeError, ValueError:
        logger.exception("reality_gate failed for %s", task.key)
        return
    ctx.evidence.reality_signal = signal
    ctx.evidence.reality_text = reality_gate.render_for_prompt(signal)
    await store.emit_event(
        task.id,
        None,
        "reality_signal",
        {
            "score": signal.get("score"),
            "confidence": signal.get("confidence"),
            "warnings": signal.get("warnings", []),
            "degraded_sources": signal.get("degraded_sources", []),
            "evidence": signal.get("evidence", []),
        },
    )
    ctx.log.note(
        "reality_gate",
        f"score={signal.get('score')} confidence={signal.get('confidence')}"
        f" warnings={len(signal.get('warnings', []))}",
    )


async def abstains(ctx: TaskContext) -> bool:
    """Block a low-evidence task before an agent run is spent on it.

    Opt-in through the ``reality_abstain_threshold`` setting (0 = off).
    """
    score = ctx.evidence.reality_signal.get("score")
    if score is None:
        return False
    await store.set_task_columns(ctx.task.id, reality_score=int(score))
    async with async_session() as db:
        threshold = await app_settings.get_int_setting(db, "reality_abstain_threshold", 0)
    if threshold <= 0 or int(score) >= threshold:
        return False

    warnings = [str(w) for w in ctx.evidence.reality_signal.get("warnings", []) or []]
    reason = f"reality score {int(score)}/100 below abstain threshold {threshold}"
    if warnings:
        reason += ": " + "; ".join(warnings[:3])
    await store.set_task_columns(ctx.task.id, abstain_reason=reason)
    await store.emit_event(
        ctx.task.id,
        None,
        "reality_abstain",
        {"score": int(score), "threshold": threshold, "reason": reason, "warnings": warnings[:5]},
    )
    await store.set_task_status(ctx.task.id, "blocked")
    ctx.log.note("abstain", f"{reason} — blocking task, no agent run")
    await notify.text(
        f"🛑 *{ctx.task.key}* abstained\n{reason}\n"
        "Provide evidence or lower `reality_abstain_threshold`, then retry."
    )
    return True


async def recall_memory(ctx: TaskContext) -> None:
    """Push a few relevant past experiences into the prompt."""
    task = ctx.task
    query = f"{task.key} {task.title}\n{task.description}"
    try:
        async with async_session() as db:
            limit = await app_settings.get_int_setting(
                db, "memory_pushed_recall_limit", DEFAULT_PUSHED_MEMORIES
            )
            if limit <= 0:
                return
            memories = await memory.search_memory_hybrid(db, ctx.repo.id, query, limit=limit)
    except SQLAlchemyError:
        # Recall is an enhancement; a missing pgvector index must not fail the task.
        logger.exception("memory_recall failed for %s", task.key)
        return
    ctx.evidence.memory_text = memory.render_memory_recall(memories)
    if not memories:
        return
    top = memories[0].get("similarity", 0.0)
    await store.emit_event(
        task.id, None, "memory_recall", {"count": len(memories), "top_similarity": top}
    )
    ctx.log.note("memory_recall", f"{len(memories)} prior entries, top sim={top:.2f}")
