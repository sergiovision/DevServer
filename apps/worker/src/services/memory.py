"""Agent memory (basic) — hybrid recall of past task experience.

Stores a short summary of every successful task in ``agent_memory`` with a
local fastembed embedding, and recalls the most relevant entries for the next
task on the same repo. Recall is *hybrid*:

    - vector lane   — pgvector cosine similarity over ``embedding``
    - lexical lane  — Postgres full-text search over ``content_tsv``

The two ranked lists are fused with Reciprocal Rank Fusion (k=60), which beats
pure cosine for code work (exact symbols, error strings, file paths). When the
embedding model is unavailable the vector lane is simply skipped; when neither
lane yields anything we fall back to the most recent entries.

Two opt-in settings keep recall fresh as the table grows (both 0 = off):

    - ``memory_decay_half_life_days`` — exponential recency decay applied to
      the fused score, so an old memory needs a stronger match to win
    - ``memory_archive_days`` — a daily sweep archives memories that were
      never recalled within that many days (see ``archive_stale_memories``)

Decisions (``memory_type='decision'``), verbatim transcript chunks
(``'transcript'``) and agent notes live in the same table; the per-repo
facade over all of it is :class:`services.repo_kb.RepoMemory`.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent_memory import AgentMemory
from services import app_settings, embeddings, secret_rules

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion constant (Cormack et al. 2009).
_RRF_K = 60


def _decay_factor(
    created_at: datetime | None, half_life_days: int, *, now: datetime | None = None
) -> float:
    """Exponential recency decay: 1.0 when fresh, 0.5 after one half-life.

    ``half_life_days <= 0`` disables decay (always 1.0).
    """
    if half_life_days <= 0 or created_at is None:
        return 1.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max(0.0, ((now or datetime.now(UTC)) - created_at).total_seconds() / 86400.0)
    return 2.0 ** (-age_days / half_life_days)


async def _bump_recall(session: AsyncSession, ids: list[int]) -> None:
    """Mark rows as recalled so the archive sweep spares them. Never raises."""
    if not ids:
        return
    try:
        await session.execute(
            text(
                "UPDATE agent_memory SET recall_count = recall_count + 1, "
                "last_recalled_at = NOW() WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        await session.commit()
    except Exception:
        # Bookkeeping only: recall itself already succeeded.
        logger.debug("recall bookkeeping update failed", exc_info=True)
        await session.rollback()


async def store_memory(
    session: AsyncSession,
    repo_id: int,
    content: str,
    memory_type: str = "experience",
    task_id: int | None = None,
    metadata: dict | None = None,
    topic: str | None = None,
) -> int:
    """Store a memory entry with an embedding (``None`` if embedding fails).

    Content is secret-redacted first: memories are recalled into future
    prompts and served over the knowledge-base lanes.
    """
    content = secret_rules.redact_secrets(content)[0]
    embedding = await embeddings.embed(content)

    memory = AgentMemory(
        repo_id=repo_id,
        task_id=task_id,
        content=content,
        embedding=embedding,
        memory_type=memory_type,
        metadata_=metadata or {},
        topic=topic,
    )
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    return memory.id


def _row_item(row, similarity: float) -> dict:
    return {
        "id": row[0],
        "content": row[1],
        "memory_type": row[2],
        "metadata": row[3],
        "created_at": str(row[4]),
        "similarity": similarity,
    }


async def _recent_memories(
    session: AsyncSession,
    repo_id: int,
    limit: int,
    *,
    memory_type: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """Fallback: newest memories for the repo, no ranking signal."""
    stmt = select(AgentMemory).where(
        AgentMemory.repo_id == repo_id,
        AgentMemory.archived_at.is_(None),
        AgentMemory.invalidated_at.is_(None),
    )
    if memory_type:
        stmt = stmt.where(AgentMemory.memory_type == memory_type)
    else:
        stmt = stmt.where(AgentMemory.memory_type != "transcript")
    if topic:
        stmt = stmt.where(AgentMemory.topic == topic)
    stmt = stmt.order_by(AgentMemory.created_at.desc()).limit(limit)
    memories = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": m.id,
            "content": m.content,
            "memory_type": m.memory_type,
            "metadata": m.metadata_,
            "created_at": str(m.created_at),
            "similarity": 0.0,
            "score": 0.0,
        }
        for m in memories
    ]


def _scope_clause(
    repo_id: int | None,
    *,
    memory_type: str | None,
    topic: str | None,
    exclude_repo_id: int | None,
) -> tuple[str, dict]:
    """WHERE clause + params shared by both recall lanes.

    Scopes to one repo, to every repo but one (cross-repo "tunnel" recall) or
    to all repos. Verbatim transcript chunks are excluded unless asked for
    explicitly with ``memory_type='transcript'``.
    """
    params: dict = {}
    if repo_id is not None:
        clauses = ["repo_id = :repo_id"]
        params["repo_id"] = repo_id
    elif exclude_repo_id is not None:
        clauses = ["repo_id <> :xrepo AND repo_id IS NOT NULL"]
        params["xrepo"] = exclude_repo_id
    else:
        clauses = ["TRUE"]
    if memory_type:
        clauses.append("memory_type = :mtype")
        params["mtype"] = memory_type
    else:
        clauses.append("memory_type <> 'transcript'")
    if topic:
        clauses.append("topic = :topic")
        params["topic"] = topic
    clauses.append("archived_at IS NULL AND invalidated_at IS NULL")
    return " AND ".join(clauses), params


async def search_memory_hybrid(
    session: AsyncSession,
    repo_id: int | None,
    query: str,
    *,
    limit: int = 5,
    memory_type: str | None = None,
    topic: str | None = None,
    exclude_repo_id: int | None = None,
    min_similarity: float = 0.0,
) -> list[dict]:
    """Hybrid recall: embedding lane + lexical (tsvector) lane fused via RRF.

    Returns dicts with ``id, content, memory_type, metadata, created_at,
    similarity, score`` — ``similarity`` is the cosine score from the vector
    lane (0.0 for lexical-only hits), ``score`` is the fused RRF score, scaled
    by recency when ``memory_decay_half_life_days`` is set. Returned rows are
    counted as recalled, which protects them from the archive sweep.

    ``memory_type`` narrows to one kind (``decision``, ``transcript``, ...);
    ``topic`` scopes to a single area of the code base. Pass ``repo_id=None``
    with ``exclude_repo_id`` to search every *other* repo. ``min_similarity``
    drops weak vector hits.
    """
    half_life = await app_settings.get_int_setting(session, "memory_decay_half_life_days", 0)
    where, base_params = _scope_clause(
        repo_id, memory_type=memory_type, topic=topic, exclude_repo_id=exclude_repo_id
    )
    cand = max(limit * 4, 20)

    items: dict[int, dict] = {}
    created: dict[int, datetime | None] = {}
    vrank: dict[int, int] = {}
    lrank: dict[int, int] = {}

    # ── Vector lane ──
    embedding = await embeddings.embed(query)
    if embedding is not None:
        vsql = text(f"""
            SELECT id, content, memory_type, metadata, created_at,
                   1 - (embedding <=> CAST(:emb AS vector)) AS similarity
            FROM agent_memory
            WHERE {where} AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:emb AS vector)
            LIMIT :lim
        """)
        try:
            rows = (
                await session.execute(vsql, {**base_params, "emb": str(embedding), "lim": cand})
            ).fetchall()
        except Exception:
            logger.debug("vector lane failed; using lexical lane only")
            await session.rollback()
            rows = []
        rank = 0
        for r in rows:
            if float(r[5]) < min_similarity:
                continue
            items[r[0]] = _row_item(r, float(r[5]))
            created[r[0]] = r[4]
            vrank[r[0]] = rank
            rank += 1

    # ── Lexical lane ──
    lsql = text(f"""
        SELECT id, content, memory_type, metadata, created_at,
               ts_rank_cd(content_tsv, websearch_to_tsquery('english', :q)) AS rank
        FROM agent_memory
        WHERE {where} AND content_tsv @@ websearch_to_tsquery('english', :q)
        ORDER BY rank DESC
        LIMIT :lim
    """)
    try:
        lrows = (await session.execute(lsql, {**base_params, "q": query, "lim": cand})).fetchall()
    except Exception:
        logger.debug("lexical lane failed; using vector lane only")
        await session.rollback()
        lrows = []
    for rank, r in enumerate(lrows):
        if r[0] not in items:
            items[r[0]] = _row_item(r, 0.0)
            created[r[0]] = r[4]
        lrank[r[0]] = rank

    if not items:
        if repo_id is None:
            return []  # the recency fallback only makes sense for one repo
        return await _recent_memories(session, repo_id, limit, memory_type=memory_type, topic=topic)

    for mid, it in items.items():
        rrf = 0.0
        if mid in vrank:
            rrf += 1.0 / (_RRF_K + vrank[mid])
        if mid in lrank:
            rrf += 1.0 / (_RRF_K + lrank[mid])
        it["score"] = rrf * _decay_factor(created.get(mid), half_life)

    hits = sorted(items.values(), key=lambda x: x["score"], reverse=True)[:limit]
    await _bump_recall(session, [it["id"] for it in hits])
    return hits


async def search_memory_iterative(
    session: AsyncSession | None,
    repo_id: int,
    query: str,
    *,
    max_iters: int = 3,
    limit: int = 5,
    llm=None,
) -> list[dict]:
    """Multi-hop recall: retrieve, ask the system LLM what is still missing, repeat.

    Up to ``max_iters`` hops, unioning and re-ranking the hits, so recall can
    chain facts a single pass would miss ("what did we pick, and why did we
    reject the alternative?"). Any LLM failure ends the hopping early and
    returns what was found so far — it never blocks a task.

    With ``session=None`` every hop opens its own short session, so no pooled
    connection is held across the planner's LLM calls; pass the already-read
    ``llm`` (:class:`services.system_llm.SystemLLM`) in that case.
    """
    from services import llm_client, system_llm

    @contextlib.asynccontextmanager
    async def _db():
        if session is not None:
            yield session
            return
        from models.base import async_session  # deferred — avoid import cycle

        async with async_session() as own:
            yield own

    if llm is None:
        async with _db() as db:
            llm = await system_llm.read(db)
    seen: dict[int, dict] = {}
    sub_query = query
    for _hop in range(max(1, max_iters)):
        async with _db() as db:
            hits = await search_memory_hybrid(db, repo_id, sub_query, limit=limit)
        for hit in hits:
            seen.setdefault(hit["id"], hit)
        if len(seen) >= limit * 2:
            break
        evidence = (
            "\n".join(f"- {(h.get('content') or '')[:160]}" for h in list(seen.values())[:8])
            or "(nothing retrieved yet)"
        )
        planner_prompt = (
            "You are helping retrieve project memories to answer a question.\n"
            f"Original question:\n{query}\n\n"
            f"Evidence gathered so far:\n{evidence}\n\n"
            "Reply with ONE short search query (max 12 words) that would fill "
            "the biggest remaining gap, or the single word DONE if the evidence "
            "is sufficient. Reply with the query only — no preamble."
        )
        try:
            nxt = (
                await llm_client.complete(
                    vendor=llm.vendor,
                    model=llm.model,
                    prompt=planner_prompt,
                    max_tokens=64,
                    timeout=30,
                    mode=llm.mode,
                )
            ).strip()
        except Exception:
            logger.debug("iterative recall planner failed; stopping early", exc_info=True)
            break
        if not nxt or nxt.upper().startswith("DONE"):
            break
        sub_query = nxt.splitlines()[0][:200]

    return sorted(seen.values(), key=lambda x: x.get("score", 0.0), reverse=True)[:limit]


def render_decision(metadata: dict) -> str:
    """A decision's causal chain (problem → alternatives → choice → why → outcome).

    Used as the embeddable ``content`` of a decision memory.
    """
    md = metadata or {}
    lines = [f"Decision: {(md.get('choice') or '').strip()}"]
    if md.get("problem"):
        lines.append(f"Problem: {md['problem']}")
    alts = md.get("alternatives") or []
    if alts:
        lines.append("Alternatives considered: " + "; ".join(str(a) for a in alts))
    if md.get("reasoning"):
        lines.append(f"Reasoning: {md['reasoning']}")
    if md.get("outcome"):
        lines.append(f"Outcome: {md['outcome']}")
    return "\n".join(lines)


async def store_decision(
    session: AsyncSession,
    repo_id: int,
    *,
    problem: str,
    choice: str,
    alternatives: list[str] | None = None,
    reasoning: str = "",
    outcome: str = "",
    task_id: int | None = None,
    extra_metadata: dict | None = None,
) -> int:
    """Store a causal decision as an ``agent_memory`` row of type ``decision``.

    The structured fields live in ``metadata``; the rendered chain becomes the
    embeddable ``content`` so recall surfaces the reasoning, not a fragment.
    """

    def _clean(value: str) -> str:
        return secret_rules.redact_secrets(value or "")[0]

    metadata = {
        **(extra_metadata or {}),
        "problem": _clean(problem),
        "alternatives": [_clean(str(a)) for a in alternatives or []],
        "choice": _clean(choice),
        "reasoning": _clean(reasoning),
        "outcome": _clean(outcome),
    }
    return await store_memory(
        session,
        repo_id=repo_id,
        content=render_decision(metadata),
        memory_type="decision",
        task_id=task_id,
        metadata=metadata,
    )


async def invalidate_memory(
    session: AsyncSession, memory_id: int, *, superseded_by: int | None = None
) -> bool:
    """Drop a memory out of recall without deleting it. Never raises."""
    try:
        result = await session.execute(
            text(
                "UPDATE agent_memory SET invalidated_at = NOW(), "
                "supersedes_id = COALESCE(:sb, supersedes_id) "
                "WHERE id = :mid AND invalidated_at IS NULL"
            ),
            {"mid": memory_id, "sb": superseded_by},
        )
        await session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]
    except Exception:
        logger.exception("invalidate_memory failed for %s", memory_id)
        await session.rollback()
        return False


async def archive_stale_memories(session: AsyncSession) -> int:
    """Archive never-recalled memories older than ``memory_archive_days``.

    Opt-in: 0 (the default) disables the sweep. Rows that were ever recalled
    are spared regardless of age. Archived rows stay in the table but are
    excluded from recall. Returns the number of rows archived; never raises.
    """
    days = await app_settings.get_int_setting(session, "memory_archive_days", 0)
    if days <= 0:
        return 0
    try:
        result = await session.execute(
            text(
                "UPDATE agent_memory SET archived_at = NOW() "
                "WHERE archived_at IS NULL AND recall_count = 0 "
                "AND created_at < NOW() - make_interval(days => :days)"
            ),
            {"days": days},
        )
        await session.commit()
        return result.rowcount or 0  # type: ignore[attr-defined]
    except Exception:
        logger.exception("archive_stale_memories failed")
        await session.rollback()
        return 0


def render_memory_recall(memories: list[dict]) -> str:
    """Render recall hits as a "Prior Experience" prompt block."""
    if not memories:
        return ""
    lines = [
        "## Prior Experience (from agent_memory)",
        "Summaries of similar past tasks — hints, not ground truth.",
    ]
    for m in memories[:3]:
        mtype = m.get("memory_type", "experience")
        sim = m.get("similarity", 0.0)
        if mtype == "decision" and isinstance(m.get("metadata"), dict):
            lines.append(f"- [decision sim={sim:.2f}] {_decision_summary(m)}")
            continue
        content = (m.get("content") or "").strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"- [{mtype} sim={sim:.2f}] {content}")
    return "\n".join(lines)


def _decision_summary(memory: dict) -> str:
    """One line: "chose X because Y (over A, B)"."""
    md = memory["metadata"]
    choice = (md.get("choice") or "").strip()
    reasoning = (md.get("reasoning") or "").strip()
    alts = md.get("alternatives") or []
    bits = [f"chose {choice}"] if choice else []
    if reasoning:
        bits.append(f"because {reasoning}")
    if alts:
        bits.append(f"(over {', '.join(str(a) for a in alts[:3])})")
    summary = " ".join(bits) or (memory.get("content") or "")
    return summary[:220] + "..." if len(summary) > 220 else summary
