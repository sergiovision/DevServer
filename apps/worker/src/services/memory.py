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
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent_memory import AgentMemory
from services import embeddings

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion constant (Cormack et al. 2009).
_RRF_K = 60


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
    lane (0.0 for lexical-only hits), ``score`` is the fused RRF score.
    """
    where = (
        "repo_id = :repo_id AND memory_type <> 'transcript' "
        "AND archived_at IS NULL AND invalidated_at IS NULL"
    )
    cand = max(limit * 4, 20)

    items: dict[int, dict] = {}
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
            rows = (await session.execute(
                vsql, {"repo_id": repo_id, "emb": str(embedding), "lim": cand}
            )).fetchall()
        except Exception:
            logger.debug("vector lane failed; using lexical lane only")
            await session.rollback()
            rows = []
        for rank, r in enumerate(rows):
            items[r[0]] = {
                "id": r[0], "content": r[1], "memory_type": r[2],
                "metadata": r[3], "created_at": str(r[4]), "similarity": float(r[5]),
            }
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
        lrows = (await session.execute(
            lsql, {"repo_id": repo_id, "q": query, "lim": cand}
        )).fetchall()
    except Exception:
        logger.debug("lexical lane failed; using vector lane only")
        await session.rollback()
        lrows = []
    for rank, r in enumerate(lrows):
        if r[0] not in items:
            items[r[0]] = {
                "id": r[0], "content": r[1], "memory_type": r[2],
                "metadata": r[3], "created_at": str(r[4]), "similarity": 0.0,
            }
        lrank[r[0]] = rank

    if not items:
        return await _recent_memories(session, repo_id, limit)

    for mid, it in items.items():
        rrf = 0.0
        if mid in vrank:
            rrf += 1.0 / (_RRF_K + vrank[mid])
        if mid in lrank:
            rrf += 1.0 / (_RRF_K + lrank[mid])
        it["score"] = rrf

    return sorted(items.values(), key=lambda x: x["score"], reverse=True)[:limit]


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
