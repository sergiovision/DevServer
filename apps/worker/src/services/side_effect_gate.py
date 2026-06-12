"""Side-effect gate — the human-in-the-loop suspend/resume state machine.

This is the runtime around :mod:`gate_classifier`. It deliberately reuses the
machinery that already exists rather than inventing a parallel one:

    suspend       → the existing ``tasks.status = 'blocked'`` (per-task only;
                    siblings keep running through PgQueuer untouched).
    notify human  → a companion ``task_messages`` row addressed to 'operator'
                    (so the open gate shows up in the existing /pro/inbox and
                    fans out via the notify dispatcher), plus ``notify.text``.
    resume        → the existing /continue path (mark in-flight runs failed
                    preserving session_id, drop the repo lock, set
                    is_continuation, re-enqueue via Next.js).

The ONLY new persistent entity is the ``decision_points`` row (migration 004).

Flow:
    1. The agent, about to do something external, POSTs to
       ``/internal/tasks/{key}/gate`` → :func:`raise_gate`.
    2. Non-blocking ⇒ allow immediately. Blocking ⇒ a decision_point is
       opened, the operator is messaged, and the agent is told to STOP.
    3. ``agent_runner.run_task`` notices the open gate after the agent exits
       (:func:`check_open_gate`) and suspends the task (:func:`suspend_for_gate`).
    4. A human resolves it from the inbox → :func:`resolve_decision` →
       approve/edit re-enqueues the task (resumes the session); reject re-enqueues
       with a "find another way" nudge.

Opt-in: every entry point short-circuits when the ``side_effect_gate`` setting
is false, so an unconfigured deployment never raises a gate.
"""

from __future__ import annotations

import json as _json
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services import app_settings, gate_classifier
from services.decomposer import _enqueue_task, _read_system_llm

logger = logging.getLogger(__name__)


async def is_enabled(session: AsyncSession) -> bool:
    return await app_settings.get_bool_setting(session, "side_effect_gate", False)


async def _emit(session: AsyncSession, task_id: int | None, event_type: str, payload: dict) -> None:
    try:
        await session.execute(
            text(
                "INSERT INTO task_events (task_id, event_type, payload) "
                "VALUES (:tid, :et, CAST(:pl AS JSONB))"
            ),
            {"tid": task_id, "et": event_type[:32], "pl": _json.dumps(payload)},
        )
    except Exception:
        logger.exception("failed to emit %s", event_type)


async def _notify_text(message: str) -> None:
    try:
        from services.notify import notify
        await notify.text(message)
    except Exception:
        logger.exception("gate notify failed")


# ─── raise (called by the agent via the /gate endpoint) ──────────────────────

async def raise_gate(
    session: AsyncSession,
    *,
    task_id: int,
    task_key: str,
    action: str,
    payload: dict | None = None,
    node_id: int | None = None,
    use_llm: bool = True,
) -> dict:
    """Classify a pending action; open a blocking decision_point if needed.

    Returns ``{"decision": "allow"|"blocked", ...}``. On ``blocked`` the caller
    (the agent) must stop — ``agent_runner`` will suspend the task when the run
    ends. Never raises.
    """
    if not await is_enabled(session):
        # Feature off → allow everything (behaviour unchanged).
        return {"decision": "allow", "kind": "disabled",
                "hint": "side_effect_gate is disabled"}

    if use_llm:
        vendor, model = await _read_system_llm(session)
        gc = await gate_classifier.classify_with_llm(action, vendor=vendor, model=model)
    else:
        gc = gate_classifier.classify(action)

    if gc.severity != "blocking":
        await _emit(session, task_id, "gate_resolved",
                    {"auto_allow": True, "kind": gc.kind, "action": action[:500]})
        await session.commit()
        return {"decision": "allow", "kind": gc.kind, "hint": gc.hint}

    resume_token = secrets.token_urlsafe(16)
    row = (await session.execute(
        text(
            """
            INSERT INTO decision_points
                (task_id, node_id, kind, severity, payload, proposed_action,
                 status, resume_token)
            VALUES
                (:task_id, :node_id, :kind, 'blocking', CAST(:pl AS JSONB),
                 :action, 'open', :rt)
            RETURNING id
            """
        ),
        {"task_id": task_id, "node_id": node_id, "kind": gc.kind,
         "pl": _json.dumps(payload or {}), "action": action[:4000], "rt": resume_token},
    )).fetchone()
    decision_id = row[0]

    # Companion operator message — reuses the existing inbox + notify fan-out.
    await session.execute(
        text(
            """
            INSERT INTO task_messages
                (from_task_id, from_task_key, to_task_id, to_task_key, kind, subject, body, payload)
            VALUES
                (:tid, :tkey, NULL, 'operator', 'request', :subject, :body, CAST(:pl AS JSONB))
            """
        ),
        {
            "tid": task_id, "tkey": task_key,
            "subject": f"Approval needed: {gc.kind}",
            "body": f"Task {task_key} wants to perform a **{gc.kind}** action:\n\n{action}\n\n"
                    f"{gc.hint}\n\nApprove or reject in the inbox (decision #{decision_id}).",
            "pl": _json.dumps({"decision_id": decision_id, "kind": gc.kind,
                               "resume_token": resume_token}),
        },
    )
    await _emit(session, task_id, "gate_raised",
                {"decision_id": decision_id, "kind": gc.kind, "hint": gc.hint,
                 "action": action[:500]})
    await session.commit()
    await _notify_text(
        f"\U0001f6d1 *{task_key}* needs approval — {gc.kind}\n{action[:300]}\n"
        f"Approve/reject decision #{decision_id} in the inbox."
    )

    return {
        "decision": "blocked",
        "decision_id": decision_id,
        "kind": gc.kind,
        "hint": gc.hint,
        "resume_token": resume_token,
        "message": (
            "This action requires human approval and has been queued for review. "
            "The task is now SUSPENDED — stop here and end your turn without "
            "performing the action. You will be resumed automatically once a "
            "human approves it (and told the decision)."
        ),
    }


# ─── observe + suspend (called by agent_runner after the agent run) ──────────

async def check_open_gate(session: AsyncSession, task_id: int) -> dict | None:
    """Return the open blocking decision for a task, or None.

    Short-circuits (no query) when the feature is disabled, so the hot retry
    loop pays nothing unless the gate is actually in use.
    """
    if not await is_enabled(session):
        return None
    row = (await session.execute(
        text(
            "SELECT id, kind, proposed_action, resume_token FROM decision_points "
            "WHERE task_id = :tid AND status = 'open' AND severity = 'blocking' "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"tid": task_id},
    )).mappings().fetchone()
    return dict(row) if row else None


async def suspend_for_gate(
    session: AsyncSession,
    *,
    task_id: int,
    run_id: int | None,
    gate: dict,
    task_log=None,
) -> None:
    """Suspend a task that has an open blocking gate: status→blocked + bookkeeping.

    The PgQueuer job ends right after run_task returns False — siblings keep
    running. Resume is via :func:`resolve_decision`.
    """
    reason = f"awaiting approval: {gate.get('kind')} — {gate.get('proposed_action', '')[:200]}"
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "UPDATE tasks SET status = 'blocked', suspended_reason = :r, "
            "suspended_at = :ts, updated_at = :ts WHERE id = :id"
        ),
        {"r": reason, "ts": now, "id": task_id},
    )
    if run_id is not None:
        await session.execute(
            text(
                "UPDATE task_runs SET status = 'failed', finished_at = :ts, "
                "error_log = :e WHERE id = :rid AND finished_at IS NULL"
            ),
            {"ts": now, "e": f"suspended for gate #{gate.get('id')}", "rid": run_id},
        )
    await session.commit()
    if task_log is not None:
        try:
            task_log.write(f"\n[gate] {reason} — task suspended (decision #{gate.get('id')})\n")
            task_log.flush()
        except Exception:
            pass


# ─── resolve (called from the inbox / endpoint) ──────────────────────────────

async def _prepare_continuation(session: AsyncSession, task_id: int) -> None:
    """Replicate the essentials of /continue: fail in-flight runs (preserving
    session_id), drop the repo lock, set is_continuation, status→pending."""
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "UPDATE task_runs SET status = 'failed', finished_at = :ts "
            "WHERE task_id = :tid AND finished_at IS NULL"
        ),
        {"ts": now, "tid": task_id},
    )
    # Release any repo lock this task holds (lock keyed by task_key).
    await session.execute(
        text(
            "DELETE FROM repo_locks WHERE task_key = "
            "(SELECT task_key FROM tasks WHERE id = :tid)"
        ),
        {"tid": task_id},
    )
    await session.execute(
        text(
            "UPDATE tasks SET is_continuation = TRUE, status = 'pending', "
            "suspended_reason = NULL, suspended_at = NULL, updated_at = :ts WHERE id = :tid"
        ),
        {"ts": now, "tid": task_id},
    )


async def resolve_decision(
    session: AsyncSession,
    *,
    decision_id: int,
    decision: str,                     # 'approve' | 'reject' | 'edit'
    comment: str = "",
    edited_payload: dict | None = None,
    resolved_by: str = "operator",
) -> dict:
    """Resolve an open decision_point and resume (or terminate) the task.

    approve/edit → re-enqueue (resumes the agent session past the gate).
    reject       → re-enqueue with a "do not perform that action" nudge.
    Returns ``{"ok", "decision_id", "status", "task_key"}``.
    """
    row = (await session.execute(
        text(
            "SELECT dp.id, dp.task_id, dp.kind, dp.proposed_action, dp.status, "
            "t.task_key FROM decision_points dp JOIN tasks t ON t.id = dp.task_id "
            "WHERE dp.id = :id"
        ),
        {"id": decision_id},
    )).mappings().fetchone()
    if not row:
        return {"ok": False, "reason": f"decision {decision_id} not found"}
    if row["status"] != "open":
        return {"ok": False, "reason": f"decision already {row['status']}"}

    task_id = row["task_id"]
    task_key = row["task_key"]
    status_map = {"approve": "approved", "reject": "rejected", "edit": "edited"}
    new_status = status_map.get(decision)
    if not new_status:
        return {"ok": False, "reason": f"unknown decision '{decision}'"}

    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "UPDATE decision_points SET status = :st, resolved_at = :ts, "
            "resolved_by = :by WHERE id = :id"
        ),
        {"st": new_status, "ts": now, "by": resolved_by, "id": decision_id},
    )
    if edited_payload is not None:
        await session.execute(
            text("UPDATE decision_points SET payload = CAST(:ep AS JSONB) WHERE id = :id"),
            {"ep": _json.dumps(edited_payload), "id": decision_id},
        )

    # Tell the agent the outcome via its inbox (it reads this on resume).
    if decision == "reject":
        body = (f"Your gated **{row['kind']}** action was REJECTED by the operator. "
                f"Do NOT perform it. {comment}\nFind an alternative or stop and report why.")
    elif decision == "edit":
        body = (f"Your gated **{row['kind']}** action was APPROVED WITH EDITS. "
                f"Use the edited parameters in the decision payload. {comment}\nProceed.")
    else:
        body = (f"Your gated **{row['kind']}** action was APPROVED. {comment}\n"
                f"Proceed with it now.")
    await session.execute(
        text(
            """
            INSERT INTO task_messages
                (from_task_id, from_task_key, to_task_id, to_task_key, kind, subject, body, payload)
            VALUES
                (NULL, 'operator', :tid, :tkey, 'response', :subject, :body, CAST(:pl AS JSONB))
            """
        ),
        {"tid": task_id, "tkey": task_key, "subject": f"Decision #{decision_id}: {new_status}",
         "body": body, "pl": _json.dumps({"decision_id": decision_id, "decision": new_status,
                                          "edited_payload": edited_payload or {}})},
    )

    await _prepare_continuation(session, task_id)
    await _emit(session, task_id, "gate_resolved",
                {"decision_id": decision_id, "status": new_status, "by": resolved_by})
    await session.commit()

    enqueued = await _enqueue_task(task_id)
    return {"ok": True, "decision_id": decision_id, "status": new_status,
            "task_key": task_key, "enqueued": enqueued}


async def list_open_decisions(session: AsyncSession, *, limit: int = 50) -> list[dict]:
    rows = (await session.execute(
        text(
            """
            SELECT dp.id, dp.task_id, t.task_key, t.title, dp.kind, dp.severity,
                   dp.proposed_action, dp.payload, dp.status, dp.created_at
            FROM decision_points dp
            JOIN tasks t ON t.id = dp.task_id
            WHERE dp.status = 'open'
            ORDER BY dp.created_at DESC
            LIMIT :lim
            """
        ),
        {"lim": limit},
    )).mappings().fetchall()
    return [dict(r) for r in rows]


# ─── prompt block (taught to the agent, settings-gated by the caller) ────────

def render_gate_prompt_block() -> list[str]:
    """Lines teaching the agent to gate side-effecting actions. Injected into
    the prompt by agent_runner only when the feature is enabled."""
    return [
        "",
        "## Side-effect approval gate (MANDATORY)",
        "Before performing ANY action that spends money, sends an external",
        "message/application, publishes content, makes a clinical/legal decision,",
        "or is irreversible (deletes data, transfers a deed), you MUST first ask",
        "for approval via the gate endpoint and then STOP if it blocks:",
        "    curl -s -X POST \\",
        "      \"$DEVSERVER_WORKER_URL/internal/tasks/$DEVSERVER_TASK_KEY/gate\" \\",
        "      -H 'content-type: application/json' \\",
        "      -d '{\"action\":\"<concise description incl. recipient/amount/url>\"}'",
        "If the response is {\"decision\":\"blocked\"}: do NOT perform the action.",
        "End your turn — you will be resumed automatically once a human approves,",
        "with the decision delivered to your inbox. If {\"decision\":\"allow\"},",
        "proceed normally. Gathering, drafting, and analysis never need a gate.",
    ]
