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
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent_memory import AgentMemory
from services import app_settings, embeddings

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
    """Store a memory entry with an embedding (``None`` if embedding fails)."""
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


async def _recent_memories(session: AsyncSession, repo_id: int, limit: int) -> list[dict]:
    """Fallback: newest memories for the repo, no ranking signal."""
    stmt = (
        select(AgentMemory)
        .where(
            AgentMemory.repo_id == repo_id,
            AgentMemory.memory_type != "transcript",
            AgentMemory.archived_at.is_(None),
            AgentMemory.invalidated_at.is_(None),
        )
        .order_by(AgentMemory.created_at.desc())
        .limit(limit)
    )
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


async def search_memory_hybrid(
    session: AsyncSession,
    repo_id: int,
    query: str,
    *,
    limit: int = 5,
) -> list[dict]:
    """Hybrid recall: embedding lane + lexical (tsvector) lane fused via RRF.

    Returns dicts with ``id, content, memory_type, metadata, created_at,
    similarity, score`` — ``similarity`` is the cosine score from the vector
    lane (0.0 for lexical-only hits), ``score`` is the fused RRF score, scaled
    by recency when ``memory_decay_half_life_days`` is set. Returned rows are
    counted as recalled, which protects them from the archive sweep.
    """
    half_life = await app_settings.get_int_setting(session, "memory_decay_half_life_days", 0)
    where = (
        "repo_id = :repo_id AND memory_type <> 'transcript' "
        "AND archived_at IS NULL AND invalidated_at IS NULL"
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
                await session.execute(
                    vsql, {"repo_id": repo_id, "emb": str(embedding), "lim": cand}
                )
            ).fetchall()
        except Exception:
            logger.debug("vector lane failed; using lexical lane only")
            await session.rollback()
            rows = []
        for rank, r in enumerate(rows):
            items[r[0]] = {
                "id": r[0],
                "content": r[1],
                "memory_type": r[2],
                "metadata": r[3],
                "created_at": str(r[4]),
                "similarity": float(r[5]),
            }
            created[r[0]] = r[4]
            vrank[r[0]] = rank

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
        lrows = (
            await session.execute(lsql, {"repo_id": repo_id, "q": query, "lim": cand})
        ).fetchall()
    except Exception:
        logger.debug("lexical lane failed; using vector lane only")
        await session.rollback()
        lrows = []
    for rank, r in enumerate(lrows):
        if r[0] not in items:
            items[r[0]] = {
                "id": r[0],
                "content": r[1],
                "memory_type": r[2],
                "metadata": r[3],
                "created_at": str(r[4]),
                "similarity": 0.0,
            }
            created[r[0]] = r[4]
        lrank[r[0]] = rank

    if not items:
        return await _recent_memories(session, repo_id, limit)

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
        content = (m.get("content") or "").strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"- [{mtype} sim={sim:.2f}] {content}")
    return "\n".join(lines)
