"""Recursive Goal Graph engine (free core).

The existing ``ideas`` tree IS the Goal Graph (migration 003 added the
decomposition columns). A node is one ``ideas`` row:

    node_type   NULL = legacy idea/folder | 'goal' | 'subtask' | 'leaf'
    node_status draft | expanding | ready | blocked | running | done | failed | abandoned
    parent_id   the tree edge (existing column)
    task_id     a leaf's bound task (existing column) + ``tasked``

This module implements the ADaPT-style *as-needed* decomposition from
``PlanImprove.md`` §B:

    expand_node()      one level: classify a node as LEAF or SPLIT (single
                       system-LLM call doubling as atomicity check +
                       plan-sketch). Leaves bind to a real ``tasks`` row and
                       enqueue through the existing pipeline; composites get
                       3–7 child nodes.
    rollup_node()      children → parent summary + 0–100 evaluator score
                       (reuses the ``compaction`` summarisation pattern).
    redetalize_node()  reopen a leaf as a subtask and decompose one level
                       further (the "improve over improve" mechanism). Wired
                       into ``agent_runner`` in a later phase.

Everything reuses ``llm_client`` (system LLM, GLM by default) and degrades
gracefully — an LLM/parse failure makes the node a leaf rather than aborting.
Leaf tasks bind to the first active repo; with no active repo the leaf is
still marked ``ready`` without a task.
"""

from __future__ import annotations

import json as _json
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services import llm_client

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 5
_MIN_CHILDREN = 2
_MAX_CHILDREN = 7
#: rollup evaluator score at/above which a parent is accepted as ``done``.
_ROLLUP_PASS_SCORE = 60


def _domain_hint(domain: str | None, config: dict | None) -> str:
    """Resolve the per-leaf domain hint dynamically — never hard-coded.

    A caller may supply a hint via ``config`` (key ``hint`` or
    ``domain_hint``); otherwise we fall back to a generic phrasing built
    from the domain string. This keeps decomposition tenant/domain-agnostic.
    """
    cfg = config if isinstance(config, dict) else {}
    hint = cfg.get("hint") or cfg.get("domain_hint")
    if hint:
        return str(hint)
    return f"a single agent action in the '{domain or 'generic'}' domain"


# ─── shared helpers ──────────────────────────────────────────────────────────

async def _read_system_llm(session: AsyncSession) -> tuple[str, str]:
    """Fetch (vendor, model) from the settings table. Defaults to GLM."""
    vendor_row = await session.execute(
        text("SELECT value FROM settings WHERE key = 'system_llm_vendor'")
    )
    model_row = await session.execute(
        text("SELECT value FROM settings WHERE key = 'system_llm_model'")
    )
    v = vendor_row.scalar_one_or_none()
    m = model_row.scalar_one_or_none()
    vendor, model = "glm", "glm-5.1"
    if v:
        try:
            vendor = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)
        except Exception:
            pass
    if m:
        try:
            model = _json.loads(m) if isinstance(m, str) and m.startswith('"') else str(m)
        except Exception:
            pass
    return vendor, model


async def _emit(session: AsyncSession, task_id: int | None, event_type: str, payload: dict) -> None:
    """Best-effort task_events insert (PG NOTIFY fires via the table trigger).

    ``task_id`` may be NULL for graph-level events not tied to a task; the
    column is nullable so the dashboard timeline still receives the NOTIFY.
    """
    try:
        await session.execute(
            text(
                "INSERT INTO task_events (task_id, event_type, payload) "
                "VALUES (:tid, :et, CAST(:pl AS JSONB))"
            ),
            {"tid": task_id, "et": event_type[:32], "pl": _json.dumps(payload)},
        )
    except Exception:
        logger.exception("failed to emit %s event", event_type)


def _parse_json_block(raw: str) -> dict | None:
    """Extract the first JSON object from an LLM reply, tolerating code fences."""
    if not raw:
        return None
    cleaned = raw.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace:
            cleaned = brace.group(0)
    try:
        obj = _json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def _load_node(session: AsyncSession, node_id: int) -> dict | None:
    row = (await session.execute(
        text(
            """
            SELECT i.id, i.parent_id, i.kind, i.title, i.content, i.task_id, i.tasked,
                   i.node_type, i.node_status, i.depth, i.sort_order
            FROM ideas i
            WHERE i.id = :nid
            """
        ),
        {"nid": node_id},
    )).mappings().fetchone()
    return dict(row) if row else None


# ─── leaf → task ─────────────────────────────────────────────────────────────

async def _create_leaf_task(
    session: AsyncSession,
    *,
    node: dict,
    title: str,
    description: str,
    acceptance: str,
) -> int | None:
    """Create a ``tasks`` row for a leaf node and link it (ideas.task_id/tasked).

    Tasks bind to the first active repo; with no active repo this returns
    None (the leaf is still marked ``ready`` by the caller). The task is
    left ``pending`` — enqueue is the operator's call, matching the
    existing convert-to-task UX.
    """
    repo_row = (await session.execute(
        text("SELECT id FROM repos WHERE active ORDER BY id LIMIT 1"),
    )).fetchone()
    repo_id = repo_row[0] if repo_row else None
    if not repo_id:
        return None

    # task_key is unique per (repo_id, task_key); the node id is globally
    # unique in ideas, so GOAL-<node_id> is stable and traceable.
    task_key = f"GOAL-{node['id']}"
    row = (await session.execute(
        text(
            """
            INSERT INTO tasks
                (repo_id, task_key, title, description, acceptance,
                 priority, status, mode, git_flow, created_by)
            VALUES
                (:repo_id, :task_key, :title, :description, :acceptance,
                 3, 'pending', 'autonomous', 'branch', 'goal-graph')
            ON CONFLICT (repo_id, task_key) DO UPDATE
                SET title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    acceptance = EXCLUDED.acceptance,
                    updated_at = NOW()
            RETURNING id
            """
        ),
        {
            "repo_id": repo_id,
            "task_key": task_key,
            "title": title[:2000],
            "description": description,
            "acceptance": acceptance,
        },
    )).fetchone()
    task_id = row[0] if row else None
    if task_id:
        await session.execute(
            text("UPDATE ideas SET tasked = TRUE, task_id = :tid WHERE id = :nid"),
            {"tid": task_id, "nid": node["id"]},
        )
    return task_id


# ─── expand ──────────────────────────────────────────────────────────────────

_EXPAND_PROMPT = """You are decomposing one node of a Goal Graph for an \
autonomous agent platform. The node belongs to the **{domain}** domain — each \
leaf is {domain_hint}.

Decide whether this node is ATOMIC (solvable in ONE focused agent run) or must \
be SPLIT into 3–7 ordered child subtasks.

Node title: {title}
Node detail:
{content}

Current depth: {depth} (max {max_depth} — at max depth you MUST return a leaf).

Reply with ONLY a JSON object, no prose, in one of these two shapes:

If atomic:
{{"leaf": true, "reason": "<one line>"}}

If it must be split:
{{"leaf": false, "reason": "<one line>",
  "children": [
    {{"title": "<short>", "description": "<what to do>",
      "acceptance": "<done criteria>", "is_leaf": true,
      "depends_on": []}}
  ]}}

Rules: 3–7 children. ``depends_on`` lists the 0-based indices of sibling \
children that must finish first (use [] for independent ones). Mark a child \
``is_leaf: true`` only if it is itself solvable in one agent run; otherwise \
``false`` so it can be split later."""


async def expand_node(
    session: AsyncSession,
    node_id: int,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    enqueue: bool = False,
) -> dict:
    """Expand one Goal Graph node one level (ADaPT-style as-needed split).

    Returns ``{"ok", "node_id", "outcome": "leaf"|"split"|"error",
    "children": [ids], "task_id", "reason"}``. Commits its own work.
    Never raises — LLM/parse failures degrade the node to a leaf.
    """
    node = await _load_node(session, node_id)
    if not node:
        return {"ok": False, "outcome": "error", "reason": f"node {node_id} not found"}

    domain = "coding"
    depth = node.get("depth") or 0
    title = node["title"]
    content = (node.get("content") or "").strip() or "(no detail provided)"

    await session.execute(
        text("UPDATE ideas SET node_status = 'expanding' WHERE id = :nid"),
        {"nid": node_id},
    )
    await session.commit()

    # Single system-LLM call = atomicity check + plan-sketch.
    plan: dict | None = None
    if depth < max_depth:
        vendor, model = await _read_system_llm(session)
        prompt = _EXPAND_PROMPT.format(
            domain=domain,
            domain_hint=_domain_hint(domain, None),
            title=title,
            content=content[:4000],
            depth=depth,
            max_depth=max_depth,
        )
        try:
            raw = await llm_client.complete(
                vendor=vendor, model=model, prompt=prompt, max_tokens=2048, timeout=120
            )
            plan = _parse_json_block(raw)
        except Exception:
            logger.exception("decompose LLM call failed for node %s", node_id)

    is_leaf = (
        depth >= max_depth
        or plan is None
        or bool(plan.get("leaf"))
        or not isinstance(plan.get("children"), list)
        or len(plan.get("children") or []) < _MIN_CHILDREN
    )

    if is_leaf:
        reason = (
            "max depth reached" if depth >= max_depth
            else (plan or {}).get("reason") or "atomic / no decomposition"
        )
        task_id = await _create_leaf_task(
            session, node=node, title=title, description=content,
            acceptance=(plan or {}).get("acceptance", "") or "",
        )
        await session.execute(
            text(
                "UPDATE ideas SET node_type = 'leaf', node_status = 'ready', "
                "stop_reason = :sr WHERE id = :nid"
            ),
            {"sr": reason, "nid": node_id},
        )
        await _emit(session, task_id, "node_leafified",
                    {"node_id": node_id, "task_id": task_id, "reason": reason,
                     "materialised": task_id is not None})
        await session.commit()
        if enqueue and task_id:
            await _enqueue_task(task_id)
        return {"ok": True, "node_id": node_id, "outcome": "leaf",
                "children": [], "task_id": task_id, "reason": reason}

    # ── SPLIT: insert children, then resolve depends_on among leaf children ──
    children = (plan.get("children") or [])[:_MAX_CHILDREN]
    parent_type = "goal" if depth == 0 else "subtask"
    inserted: list[dict] = []  # {idx, id, is_leaf, task_id, depends_idx}

    for idx, child in enumerate(children):
        c_title = str(child.get("title") or f"Subtask {idx + 1}")[:256]
        c_desc = str(child.get("description") or "")
        c_accept = str(child.get("acceptance") or "")
        c_is_leaf = bool(child.get("is_leaf"))
        c_content = c_desc + (f"\n\n**Acceptance:** {c_accept}" if c_accept else "")
        c_status = "ready" if c_is_leaf else "draft"
        c_node_type = "leaf" if c_is_leaf else "subtask"

        crow = (await session.execute(
            text(
                """
                INSERT INTO ideas
                    (parent_id, kind, title, content,
                     node_type, node_status, depth, sort_order)
                VALUES
                    (:pid, 'idea', :title, :content,
                     :ntype, :nstatus, :depth, :sort)
                RETURNING id
                """
            ),
            {
                "pid": node_id,
                "title": c_title,
                "content": c_content,
                "ntype": c_node_type,
                "nstatus": c_status,
                "depth": depth + 1,
                "sort": idx,
            },
        )).fetchone()
        child_id = crow[0]

        task_id = None
        if c_is_leaf:
            child_node = await _load_node(session, child_id)
            task_id = await _create_leaf_task(
                session, node=child_node, title=c_title,
                description=c_desc or c_title, acceptance=c_accept,
            )

        depends_idx = child.get("depends_on") if isinstance(child.get("depends_on"), list) else []
        inserted.append({"idx": idx, "id": child_id, "is_leaf": c_is_leaf,
                         "task_id": task_id, "depends_idx": depends_idx})

    # Resolve sibling dependencies → tasks.depends_on (only edges between
    # materialised leaf tasks; subtask deps are honoured once they leafify).
    idx_to_task = {c["idx"]: c["task_id"] for c in inserted if c["task_id"]}
    for c in inserted:
        if not c["task_id"]:
            continue
        dep_task_ids = [idx_to_task[i] for i in c["depends_idx"] if i in idx_to_task and idx_to_task[i] != c["task_id"]]
        if dep_task_ids:
            await session.execute(
                text("UPDATE tasks SET depends_on = :dep WHERE id = :tid"),
                {"dep": dep_task_ids, "tid": c["task_id"]},
            )

    await session.execute(
        text(
            "UPDATE ideas SET node_type = :nt, node_status = 'expanding', "
            "expand_reason = :er WHERE id = :nid"
        ),
        {"nt": parent_type, "er": plan.get("reason") or "", "nid": node_id},
    )
    await _emit(session, None, "goal_expanded",
                {"node_id": node_id, "children": [c["id"] for c in inserted],
                 "count": len(inserted), "reason": plan.get("reason")})
    await session.commit()

    if enqueue:
        for c in inserted:
            if c["task_id"] and not c["depends_idx"]:
                await _enqueue_task(c["task_id"])

    return {"ok": True, "node_id": node_id, "outcome": "split",
            "children": [c["id"] for c in inserted], "task_id": None,
            "reason": plan.get("reason")}


# ─── rollup ──────────────────────────────────────────────────────────────────

_ROLLUP_PROMPT = """You are rolling up the results of child subtasks into their \
parent node's summary, for an autonomous agent platform.

Parent goal: {title}
Parent detail:
{content}

Children (all complete):
{children_block}

Produce ONLY a JSON object:
{{"summary": "<concise markdown synthesis of what was achieved>",
  "score": <0-100 integer: how fully the children satisfy the parent goal>,
  "evidence": "<one line citing the strongest supporting child result>"}}"""


async def rollup_node(session: AsyncSession, node_id: int) -> dict:
    """Synthesise completed children into the parent's rollup_summary + score.

    Only runs when every non-abandoned child is ``done``. On pass (score ≥
    threshold) the parent → ``done``; otherwise → ``failed`` with a reason.
    Returns ``{"ok", "node_id", "status", "score", "summary", "reason"}``.
    """
    node = await _load_node(session, node_id)
    if not node:
        return {"ok": False, "reason": f"node {node_id} not found"}

    children = (await session.execute(
        text(
            """
            SELECT id, title, node_status, rollup_summary, content, task_id
            FROM ideas
            WHERE parent_id = :pid AND node_type IS NOT NULL
            ORDER BY sort_order, id
            """
        ),
        {"pid": node_id},
    )).mappings().fetchall()

    active = [c for c in children if c["node_status"] != "abandoned"]
    if not active:
        return {"ok": False, "node_id": node_id, "reason": "no active children to roll up"}
    not_done = [c["id"] for c in active if c["node_status"] != "done"]
    if not_done:
        return {"ok": False, "node_id": node_id, "reason": "children not all done",
                "pending": not_done}

    # Build the children evidence block (prefer a child's own rollup summary).
    blocks = []
    for c in active:
        body = (c["rollup_summary"] or c["content"] or "").strip()[:1500]
        blocks.append(f"### {c['title']}\n{body or '(no summary)'}")
    children_block = "\n\n".join(blocks)

    vendor, model = await _read_system_llm(session)
    prompt = _ROLLUP_PROMPT.format(
        title=node["title"],
        content=(node.get("content") or "")[:2000] or "(no detail)",
        children_block=children_block,
    )
    summary, score, evidence = "", _ROLLUP_PASS_SCORE, ""
    try:
        raw = await llm_client.complete(
            vendor=vendor, model=model, prompt=prompt, max_tokens=2048, timeout=120
        )
        parsed = _parse_json_block(raw) or {}
        summary = str(parsed.get("summary") or "").strip()
        evidence = str(parsed.get("evidence") or "").strip()
        try:
            score = int(parsed.get("score"))
        except (TypeError, ValueError):
            score = _ROLLUP_PASS_SCORE
    except Exception:
        logger.exception("rollup LLM call failed for node %s", node_id)
        summary = "Children completed; automatic synthesis unavailable."

    score = max(0, min(100, score))
    passed = score >= _ROLLUP_PASS_SCORE
    new_status = "done" if passed else "failed"
    stop_reason = "" if passed else f"rollup score {score} below {_ROLLUP_PASS_SCORE}"

    await session.execute(
        text(
            "UPDATE ideas SET node_status = :st, evaluator_score = :sc, "
            "rollup_summary = :rs, stop_reason = :sr WHERE id = :nid"
        ),
        {"st": new_status, "sc": score,
         "rs": (summary + (f"\n\n_evidence: {evidence}_" if evidence else "")),
         "sr": stop_reason, "nid": node_id},
    )
    await _emit(session, node.get("task_id"), "node_rolled_up",
                {"node_id": node_id, "score": score, "status": new_status})
    await session.commit()
    return {"ok": True, "node_id": node_id, "status": new_status,
            "score": score, "summary": summary, "reason": stop_reason}


# ─── redetalize (improve-over-improve; wired into agent_runner in a later phase) ─

async def redetalize_node(session: AsyncSession, node_id: int, *, reason: str = "") -> dict:
    """Reopen a failed/over-budget leaf as a subtask and decompose one level.

    The bound task (if any) keeps its history; the node becomes a composite
    and is expanded again. Returns the ``expand_node`` result.
    """
    node = await _load_node(session, node_id)
    if not node:
        return {"ok": False, "outcome": "error", "reason": f"node {node_id} not found"}
    await session.execute(
        text(
            "UPDATE ideas SET node_type = 'subtask', node_status = 'expanding', "
            "tasked = FALSE, task_id = NULL, "
            "expand_reason = :er WHERE id = :nid"
        ),
        {"er": f"re-detalized: {reason}"[:2000], "nid": node_id},
    )
    await _emit(session, node.get("task_id"), "node_leafified",
                {"node_id": node_id, "redetalize": True, "reason": reason})
    await session.commit()
    return await expand_node(session, node_id)


# ─── enqueue (best-effort; mirrors night_cycle's Next.js handoff) ────────────

_WEB_PORT = 3000


async def _enqueue_task(task_id: int) -> bool:
    """POST to the Next.js enqueue endpoint (single source of truth for the queue)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(f"http://localhost:{_WEB_PORT}/api/tasks/{task_id}/enqueue")
            return resp.status_code == 200
    except Exception:
        logger.exception("failed to enqueue goal-graph task %d", task_id)
        return False
