"""Temporal facts: subject → predicate → object, with validity windows.

Memories only *decay*; facts get *invalidated*. Recording a new fact for the
same ``(repo_id, subject, predicate)`` closes the previous one (sets
``valid_until``, ``invalidated_at`` and ``superseded_by``), so recall always
reflects current truth while the full history stays queryable for "what did
we believe, and when did that change?".

Reached through :class:`services.repo_kb.RepoMemory`. The write paths never
raise — a fact-store failure must not break the task that recorded it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services import embeddings, secret_rules
from services.runner import store

logger = logging.getLogger(__name__)

_SELECT_COLS = (
    "id, subject, predicate, object, topic, confidence, valid_from, valid_until, source_task_id"
)


def fact_text(subject: str, predicate: str, obj: str) -> str:
    return f"{subject} {predicate} {obj}".strip()


def _vector(vec: list[float] | None) -> str | None:
    return str(list(vec)) if vec is not None else None


def _redact(value: str) -> str:
    return secret_rules.redact_secrets(value or "")[0]


async def record_fact(
    session: AsyncSession,
    repo_id: int,
    subject: str,
    predicate: str,
    obj: str,
    *,
    topic: str | None = None,
    confidence: float = 1.0,
    source_task_id: int | None = None,
    metadata: dict | None = None,
) -> int:
    """Insert a fact and supersede the current one for the same subject+predicate.

    Returns the new fact id, or 0 on failure.
    """
    subject, predicate, obj = _redact(subject), _redact(predicate), _redact(obj)
    try:
        emb = await embeddings.embed(fact_text(subject, predicate, obj))
        # Serialise writers of the same subject+predicate: without it two
        # concurrent inserts each supersede only the *older* row and both stay
        # current. The lock is released at commit.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"memory_facts:{repo_id}:{subject}:{predicate}"},
        )
        new_id = (
            await session.execute(
                text(
                    "INSERT INTO memory_facts "
                    "(repo_id, subject, predicate, object, embedding, topic, "
                    " confidence, source_task_id, metadata) "
                    "VALUES (:rid, :s, :p, :o, CAST(:emb AS vector), :topic, "
                    " :conf, :tid, CAST(:meta AS JSONB)) RETURNING id"
                ),
                {
                    "rid": repo_id,
                    "s": subject,
                    "p": predicate,
                    "o": obj,
                    "emb": _vector(emb),
                    "topic": topic,
                    "conf": confidence,
                    "tid": source_task_id,
                    "meta": json.dumps(metadata or {}),
                },
            )
        ).scalar_one()

        await session.execute(
            text(
                "UPDATE memory_facts SET valid_until = NOW(), "
                "invalidated_at = NOW(), superseded_by = :nid "
                "WHERE repo_id = :rid AND subject = :s AND predicate = :p "
                "AND valid_until IS NULL AND id <> :nid"
            ),
            {"nid": new_id, "rid": repo_id, "s": subject, "p": predicate},
        )
        await session.commit()
    except Exception:
        logger.exception("record_fact failed (repo %s)", repo_id)
        await session.rollback()
        return 0
    if source_task_id is not None:
        try:
            await store.emit_event(
                source_task_id,
                None,
                "fact_recorded",
                {
                    "fact_id": new_id,
                    "subject": subject[:80],
                    "predicate": predicate[:80],
                    "object": obj[:120],
                },
            )
        except Exception:
            logger.debug("fact_recorded event failed (fact %s)", new_id, exc_info=True)
    return int(new_id)


async def invalidate_fact(session: AsyncSession, fact_id: int, *, reason: str = "") -> bool:
    """Mark a fact no longer true. True when a current fact was closed."""
    try:
        row = (
            await session.execute(
                text(
                    "UPDATE memory_facts SET valid_until = COALESCE(valid_until, NOW()), "
                    "invalidated_at = NOW() "
                    "WHERE id = :fid AND invalidated_at IS NULL "
                    "RETURNING source_task_id"
                ),
                {"fid": fact_id},
            )
        ).fetchone()
        await session.commit()
    except Exception:
        logger.exception("invalidate_fact failed (fact %s)", fact_id)
        await session.rollback()
        return False
    if row is None:
        return False
    if row[0] is not None:
        try:
            await store.emit_event(
                row[0],
                None,
                "fact_invalidated",
                {"fact_id": fact_id, "reason": (reason or "")[:200]},
            )
        except Exception:
            logger.debug("fact_invalidated event failed (fact %s)", fact_id, exc_info=True)
    return True


def _row_to_fact(r) -> dict:
    return {
        "id": r[0],
        "subject": r[1],
        "predicate": r[2],
        "object": r[3],
        "topic": r[4],
        "confidence": float(r[5]) if r[5] is not None else None,
        "valid_from": str(r[6]) if r[6] else None,
        "valid_until": str(r[7]) if r[7] else None,
        "source_task_id": r[8],
    }


async def query_facts(
    session: AsyncSession,
    repo_id: int,
    *,
    subject: str | None = None,
    predicate: str | None = None,
    at_time: datetime | None = None,
    include_invalid: bool = False,
    limit: int = 50,
) -> list[dict]:
    """A repo's facts: current only by default, as of ``at_time`` when given,
    or the full history with ``include_invalid=True``. Never raises."""
    where = ["repo_id = :rid"]
    params: dict = {"rid": repo_id, "lim": limit}
    if subject:
        where.append("subject = :s")
        params["s"] = subject
    if predicate:
        where.append("predicate = :p")
        params["p"] = predicate
    if at_time is not None:
        where.append("valid_from <= :at AND (valid_until IS NULL OR valid_until > :at)")
        params["at"] = at_time
    elif not include_invalid:
        where.append("valid_until IS NULL")
    sql = (
        f"SELECT {_SELECT_COLS} FROM memory_facts WHERE {' AND '.join(where)} "
        "ORDER BY valid_from DESC, id DESC LIMIT :lim"
    )
    try:
        rows = (await session.execute(text(sql), params)).fetchall()
    except Exception:
        logger.exception("query_facts failed (repo %s)", repo_id)
        await session.rollback()
        return []
    return [_row_to_fact(r) for r in rows]


async def timeline(
    session: AsyncSession, repo_id: int, subject: str, *, limit: int = 50
) -> list[dict]:
    """Chronological history of a subject's facts, invalidated ones included."""
    try:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_SELECT_COLS} FROM memory_facts "
                    "WHERE repo_id = :rid AND subject = :s "
                    "ORDER BY valid_from ASC, id ASC LIMIT :lim"
                ),
                {"rid": repo_id, "s": subject, "lim": limit},
            )
        ).fetchall()
    except Exception:
        logger.exception("timeline failed (repo %s)", repo_id)
        await session.rollback()
        return []
    return [_row_to_fact(r) for r in rows]


async def search_facts(
    session: AsyncSession, repo_id: int, query: str, *, limit: int = 8
) -> list[dict]:
    """Semantic search over current facts; the newest facts without a model."""
    emb = await embeddings.embed(query)
    if emb is None:
        return await query_facts(session, repo_id, limit=limit)
    try:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_SELECT_COLS} FROM memory_facts "
                    "WHERE repo_id = :rid AND valid_until IS NULL AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:emb AS vector) LIMIT :lim"
                ),
                {"rid": repo_id, "emb": _vector(emb), "lim": limit},
            )
        ).fetchall()
    except Exception:
        logger.exception("search_facts failed (repo %s)", repo_id)
        await session.rollback()
        return []
    return [_row_to_fact(r) for r in rows]


def render_facts(facts: list[dict]) -> str:
    """Current facts as a compact "Known Facts" prompt block."""
    if not facts:
        return ""
    lines = [
        "## Known Facts (current project knowledge)",
        "Established facts — treat as ground truth unless you find evidence "
        "otherwise (then record the correction).",
    ]
    lines += [f"- {f['subject']} {f['predicate']} {f['object']}" for f in facts[:12]]
    return "\n".join(lines)
