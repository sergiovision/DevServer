"""Run a follow-up action when a task reaches a terminal status.

A task (or the template it came from) may carry ``post_completion_action``
(migration 004): re-run another task, or start a new ``skill`` task in the
same repo, on success, on failure or always. It removes the "watch the board
and click Run on the next task" chore for short pipelines without becoming a
workflow engine — one hop per completion, nothing more.

Invariants:

* **Opt-in.** A task without an action does nothing and emits nothing, so an
  unconfigured deployment behaves exactly as before.
* **Never fails the source.** Every error is logged and recorded as a
  ``post_completion_action`` event; the source task's status is never touched.
* **Loop guard.** ``tasks.triggered_by_task_id`` links a triggered task to the
  task whose completion started it. A task never triggers itself, never
  triggers a task already in its own chain, and a chain stops after
  :data:`MAX_CHAIN_DEPTH` automatic hops.
* **Single queue producer.** Enqueueing goes through
  :func:`services.queue_bridge.enqueue_via_web`; the worker never writes to
  ``pgqueuer``.
* ``cancelled`` never triggers anything: an operator who stops a task wants it
  stopped, not handed to the next one.

The decision is the pure :func:`decide_post_completion`; :func:`after_completion`
is the thin shell that reads the row, decides, acts and records the outcome,
opening one short session per unit of work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from models.base import async_session
from services import queue_bridge
from services.runner import store

logger = logging.getLogger(__name__)

EVENT_TYPE = "post_completion_action"

#: At most this many automatic hops from a task an operator started.
MAX_CHAIN_DEPTH = 3

ActionKind = Literal["task", "skill"]
Trigger = Literal["on_success", "on_failure", "always"]

#: Name → literal, so a parsed string narrows to the ``Trigger`` type.
_TRIGGERS: dict[str, Trigger] = {
    "on_success": "on_success",
    "on_failure": "on_failure",
    "always": "always",
}

#: ``done`` is written by the operator, never by the runner, but it is a
#: success all the same if a follow-up ever sees it.
SUCCESS_STATUSES: frozenset[str] = frozenset({"test", "done"})
FAILURE_STATUSES: frozenset[str] = frozenset({"failed", "blocked"})

#: Pseudo-status for a task parked by the side-effect gate: ``blocked`` on the
#: row, but it resumes once the operator decides, so it is not an outcome.
SUSPENDED = "suspended"


@dataclass(frozen=True)
class PostCompletionAction:
    """A validated ``tasks.post_completion_action`` value."""

    kind: ActionKind
    trigger: Trigger
    task_id: int | None = None
    skill_name: str | None = None


@dataclass(frozen=True)
class Decision:
    """Whether to fire, and a reason readable on the task's event log."""

    fire: bool
    reason: str


def parse_action(raw: object) -> PostCompletionAction | None:
    """Parse a stored action; None for anything absent or malformed.

    The web tier validates on write, but the column is plain JSONB and may be
    edited by hand, so the worker re-checks rather than trusting the shape.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    trigger_name = raw.get("trigger")
    if not isinstance(trigger_name, str) or trigger_name not in _TRIGGERS:
        return None
    trigger = _TRIGGERS[trigger_name]
    if kind == "task":
        task_id = raw.get("task_id")
        # bool is an int subclass; True is not a task id.
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            return None
        return PostCompletionAction(kind="task", trigger=trigger, task_id=task_id)
    if kind != "skill":
        return None  # 'script' and anything else is deliberately unsupported
    skill_name = raw.get("skill_name")
    if not isinstance(skill_name, str) or not skill_name.strip():
        return None
    return PostCompletionAction(kind="skill", trigger=trigger, skill_name=skill_name.strip())


def trigger_matches(trigger: Trigger, finished_status: str) -> bool:
    """True when ``finished_status`` is an outcome ``trigger`` responds to."""
    if finished_status in SUCCESS_STATUSES:
        return trigger in ("on_success", "always")
    if finished_status in FAILURE_STATUSES:
        return trigger in ("on_failure", "always")
    return False


def chain_is_cyclic(ancestors: Sequence[int]) -> bool:
    """True when the ``triggered_by`` walk revisited a task."""
    return len(set(ancestors)) != len(ancestors)


def decide_post_completion(
    action: PostCompletionAction | None,
    finished_status: str,
    source_task_id: int,
    ancestors: Sequence[int],
) -> Decision:
    """Decide whether the source task's completion fires its action.

    ``ancestors`` is the ``triggered_by_task_id`` chain above the source,
    nearest first (empty for a task the operator started). The task the
    action starts would sit at depth ``len(ancestors) + 1``.
    """
    if action is None:
        return Decision(False, "no action configured")
    if finished_status == "cancelled":
        return Decision(False, "cancelled tasks never trigger an action")
    if finished_status not in SUCCESS_STATUSES | FAILURE_STATUSES:
        return Decision(False, f"status '{finished_status}' is not a completion")
    if not trigger_matches(action.trigger, finished_status):
        return Decision(False, f"trigger '{action.trigger}' does not match '{finished_status}'")
    if action.kind == "task":
        if action.task_id == source_task_id:
            return Decision(False, "a task cannot trigger itself")
        if action.task_id in ancestors:
            return Decision(False, f"task {action.task_id} is already in this chain (loop)")
    if source_task_id in ancestors or chain_is_cyclic(ancestors):
        return Decision(False, "trigger chain contains a loop")
    if len(ancestors) + 1 > MAX_CHAIN_DEPTH:
        return Decision(False, f"chain depth cap {MAX_CHAIN_DEPTH} reached")
    return Decision(True, f"'{finished_status}' matches '{action.trigger}'")


# ─── The shell: read, decide, act, record ────────────────────────────────────

_background: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class _Source:
    raw_action: object
    status: str
    suspended: bool


async def after_completion(task_id: int) -> None:
    """Fire ``task_id``'s action if its terminal status calls for it. Never raises.

    The row is read inline (one short query), so a task without an action
    costs nothing more and leaves no background work behind. The act itself
    is detached: the runner calls this from ``finalise`` while it still holds
    the repo lock, so a follow-up in the same repo enqueued inline would only
    hit ``RepoLockBusy`` and wait for the consumer's requeue delay; detached,
    it usually finds the lock released. It also keeps the follow-up out of a
    cancelled run's task, whose awaits would be interrupted.
    """
    try:
        source = await _load_source(task_id)
    except Exception as exc:
        # Typically a database that has not run migration 004 yet: one line,
        # not a traceback per finished task.
        logger.warning("could not read the post-completion action of task %d: %s", task_id, exc)
        return
    if source is None or source.raw_action is None:
        return  # opt-in: nothing configured, nothing recorded
    try:
        job = asyncio.get_running_loop().create_task(
            _fire_quietly(task_id, source), name=f"post-completion-{task_id}"
        )
    except RuntimeError:
        logger.exception("could not schedule the post-completion action of task %d", task_id)
        return
    _background.add(job)
    job.add_done_callback(_background.discard)


async def _fire_quietly(task_id: int, source: _Source) -> None:
    try:
        await _fire(task_id, source)
    except Exception as exc:
        logger.exception("post-completion action for task %d failed", task_id)
        await _record(task_id, fired=False, reason=f"error: {exc}"[:500])


async def _fire(task_id: int, source: _Source) -> None:
    action = parse_action(source.raw_action)
    if action is None:
        await _record(task_id, fired=False, reason="malformed post_completion_action")
        return

    parked = source.status == "blocked" and source.suspended
    finished_status = SUSPENDED if parked else source.status
    ancestors = await _ancestors(task_id)
    decision = decide_post_completion(action, finished_status, task_id, ancestors)
    if not decision.fire:
        await _record(task_id, fired=False, reason=decision.reason, action=action)
        return

    if action.kind == "task":
        target_id, refusal = await _reset_target(action.task_id or 0, task_id)
    else:
        target_id, refusal = await _create_skill_task(action.skill_name or "", task_id)
    if target_id is None:
        await _record(task_id, fired=False, reason=refusal, action=action)
        return

    enqueued = await queue_bridge.enqueue_via_web(target_id, triggered=True)
    await _record(
        task_id,
        fired=True,
        reason=decision.reason if enqueued else "target is pending but the enqueue failed",
        action=action,
        target_task_id=target_id,
        enqueued=enqueued,
    )


async def _load_source(task_id: int) -> _Source | None:
    async with async_session() as db:
        row = (
            await db.execute(
                text(
                    "SELECT post_completion_action, status, suspended_reason "
                    "FROM tasks WHERE id = :id"
                ),
                {"id": task_id},
            )
        ).one_or_none()
    if row is None:
        return None
    return _Source(
        raw_action=row.post_completion_action,
        status=row.status or "",
        suspended=row.suspended_reason is not None,
    )


async def _ancestors(task_id: int) -> list[int]:
    """Walk ``triggered_by_task_id`` upwards, nearest first.

    Stops one step past the depth cap (enough to refuse) or at the first
    revisited id, which is kept so the pure check sees the cycle.
    """
    chain: list[int] = []
    current = task_id
    async with async_session() as db:
        for _ in range(MAX_CHAIN_DEPTH + 1):
            parent = (
                await db.execute(
                    text("SELECT triggered_by_task_id FROM tasks WHERE id = :id"),
                    {"id": current},
                )
            ).scalar_one_or_none()
            if parent is None:
                break
            chain.append(parent)
            if parent == task_id or chain.count(parent) > 1:
                break
            current = parent
    return chain


async def _reset_target(target_id: int, source_id: int) -> tuple[int | None, str]:
    """Put the target back to ``pending``; refuse one already in flight.

    The status check and the update are one statement, so a target that is
    enqueued concurrently is never reset under its own run.
    """
    async with async_session() as db:
        updated = (
            await db.execute(
                text(
                    "UPDATE tasks SET status = 'pending', triggered_by_task_id = :source, "
                    "queue_job_id = NULL, updated_at = NOW() "
                    # A target already queued/running/verifying is on its way;
                    # resetting it would race the run in flight.
                    "WHERE id = :target "
                    "AND COALESCE(status, 'pending') NOT IN ('queued', 'running', 'verifying') "
                    "RETURNING id"
                ),
                {"source": source_id, "target": target_id},
            )
        ).scalar_one_or_none()
        if updated is None:
            status = (
                await db.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": target_id})
            ).scalar_one_or_none()
            await db.rollback()
            if status is None:
                return None, f"target task {target_id} does not exist"
            return None, f"target task {target_id} is already {status}"
        await db.commit()
    return target_id, ""


async def _create_skill_task(skill_name: str, source_id: int) -> tuple[int | None, str]:
    """Insert a ``skill`` task in the source's repo, linked back to the source."""
    async with async_session() as db:
        source = (
            await db.execute(
                text(
                    "SELECT task_key, repo_id, priority, claude_mode, agent_vendor, "
                    "claude_model FROM tasks WHERE id = :id"
                ),
                {"id": source_id},
            )
        ).one_or_none()
        if source is None:
            return None, "source task disappeared"
        skill_id = (
            await db.execute(text("SELECT id FROM skills WHERE name = :n"), {"n": skill_name})
        ).scalar_one_or_none()
        if skill_id is None:
            return None, f"unknown skill '{skill_name}' (sync skills first)"
        # Unique per (repo_id, task_key); the random suffix keeps repeated
        # completions of the same source from colliding.
        task_key = f"PCA-{source_id}-{secrets.token_hex(3).upper()}"
        new_id = (
            await db.execute(
                text(
                    """
                    INSERT INTO tasks
                        (repo_id, task_key, title, description, priority, status, mode,
                         task_type, claude_mode, agent_vendor, claude_model, skill_id,
                         triggered_by_task_id, created_by)
                    VALUES
                        (:repo_id, :task_key, :title, :description, :priority, 'pending',
                         'autonomous', 'skill', :claude_mode, :agent_vendor, :claude_model,
                         :skill_id, :source_id, 'post-completion')
                    RETURNING id
                    """
                ),
                {
                    "repo_id": source.repo_id,
                    "task_key": task_key,
                    "title": f"Run skill {skill_name} after {source.task_key}",
                    "description": (
                        f"Run the '{skill_name}' skill. Triggered by the completion of "
                        f"task {source.task_key}."
                    ),
                    "priority": source.priority or 3,
                    "claude_mode": source.claude_mode or "max",
                    "agent_vendor": source.agent_vendor or "anthropic",
                    "claude_model": source.claude_model,
                    "skill_id": skill_id,
                    "source_id": source_id,
                },
            )
        ).scalar_one()
        await db.commit()
    return int(new_id), ""


async def _record(
    task_id: int,
    *,
    fired: bool,
    reason: str,
    action: PostCompletionAction | None = None,
    target_task_id: int | None = None,
    enqueued: bool | None = None,
) -> None:
    payload: dict[str, object] = {"fired": fired, "reason": reason}
    if action is not None:
        payload.update(kind=action.kind, trigger=action.trigger)
        if action.task_id is not None:
            payload["task_id"] = action.task_id
        if action.skill_name is not None:
            payload["skill_name"] = action.skill_name
    if target_task_id is not None:
        payload["target_task_id"] = target_task_id
    if enqueued is not None:
        payload["enqueued"] = enqueued
    try:
        await store.emit_event(task_id, None, EVENT_TYPE, payload)
    except Exception:
        logger.exception("could not record post-completion event for task %d", task_id)
    logger.info("post-completion action for task %d: fired=%s (%s)", task_id, fired, reason)
