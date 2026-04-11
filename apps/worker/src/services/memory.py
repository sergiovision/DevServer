"""Agent memory — pgvector similarity search for experience recall."""

import logging

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.agent_memory import AgentMemory

logger = logging.getLogger(__name__)


async def _get_embedding(content: str) -> list[float] | None:
    """Get embedding vector from Anthropic/OpenAI-compatible API.

    Uses the Voyage AI embeddings endpoint (Anthropic partner).
    Falls back to None if unavailable.
    """
    if not settings.anthropic_api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {settings.anthropic_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "voyage-code-3",
                    "input": content[:8000],
                },
            )
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception:
        logger.debug("Embedding generation failed, storing without vector")
        return None


async def store_memory(
    session: AsyncSession,
    repo_id: int,
    content: str,
    memory_type: str = "experience",
    task_id: int | None = None,
    metadata: dict | None = None,
) -> int:
    """Store a memory entry with optional embedding."""
    embedding = await _get_embedding(content)

    memory = AgentMemory(
        repo_id=repo_id,
        task_id=task_id,
        content=content,
        embedding=embedding,
        memory_type=memory_type,
        metadata_=metadata or {},
    )
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    return memory.id


async def search_memory(
    session: AsyncSession,
    repo_id: int,
    query: str,
    memory_type: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search memories by cosine similarity. Falls back to text search."""
    embedding = await _get_embedding(query)

    if embedding is not None:
        # Vector similarity search
        type_filter = "AND memory_type = :mtype" if memory_type else ""
        sql = text(f"""
            SELECT id, content, memory_type, metadata, created_at,
                   1 - (embedding <=> :emb::vector) AS similarity
            FROM agent_memory
            WHERE repo_id = :repo_id {type_filter}
            AND embedding IS NOT NULL
            ORDER BY embedding <=> :emb::vector
            LIMIT :lim
        """)
        params = {"repo_id": repo_id, "emb": str(embedding), "lim": limit}
        if memory_type:
            params["mtype"] = memory_type

        result = await session.execute(sql, params)
        rows = result.fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "memory_type": r[2],
                "metadata": r[3],
                "created_at": str(r[4]),
                "similarity": float(r[5]),
            }
            for r in rows
        ]

    # Fallback: simple text search
    stmt = select(AgentMemory).where(AgentMemory.repo_id == repo_id)
    if memory_type:
        stmt = stmt.where(AgentMemory.memory_type == memory_type)
    stmt = stmt.order_by(AgentMemory.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    memories = result.scalars().all()
    return [
        {
            "id": m.id,
            "content": m.content,
            "memory_type": m.memory_type,
            "metadata": m.metadata_,
            "created_at": str(m.created_at),
            "similarity": 0.0,
        }
        for m in memories
    ]
