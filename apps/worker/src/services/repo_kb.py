"""Per-repo Memory Knowledge Base — the single entry point for one repo's memory.

``RepoMemory(session, repo_id)`` gathers everything a repo remembers:

    - recall          — hybrid (vector + lexical) recall of experience,
                        decisions and notes, optionally reranked by the system
                        LLM or widened to other repos ("tunnels")
    - transcripts     — verbatim chunks of past runs, searched on demand
    - decisions       — causal chains (problem → choice → why)
    - facts           — subject/predicate/object with supersession
                        (:mod:`services.memory_facts`)
    - wake-up digest  — a small cached summary injected on a task's first
                        attempt (repo identity, current facts, recent decisions)

It is cheap to construct (it only holds the session and the repo id), so call
sites build one on demand. Storage lives in ``agent_memory``,
``memory_facts`` and ``repo_memory``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services import app_settings, embeddings, memory, memory_facts, secret_rules
from services.runner import store

logger = logging.getLogger(__name__)

# Verbatim transcript chunking: char windows with overlap so a fact split
# across a boundary still survives whole in one chunk.
_CHUNK_CHARS = 2000
_CHUNK_OVERLAP = 256

#: Cross-repo hits count half as much as the repo's own memories.
_TUNNEL_WEIGHT = 0.5

#: Transcript archiving bounds: a runaway transcript must not turn one task's
#: success into thousands of embeddings. Each run keeps its *last* chunks (the
#: outcome lives at the end); embedding happens in fixed-size batches.
_MAX_TRANSCRIPT_CHUNKS_PER_RUN = 40
_MAX_TRANSCRIPT_CHUNKS_PER_TASK = 200
_TRANSCRIPT_EMBED_BATCH = 32


def chunk_text(s: str, size: int = _CHUNK_CHARS, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping char windows. Empty input → []."""
    s = s or ""
    if not s.strip():
        return []
    out: list[str] = []
    start, n = 0, len(s)
    while start < n:
        end = min(start + size, n)
        out.append(s[start:end])
        if end >= n:
            break
        start = end - overlap
    return out


def parse_rerank_indices(reply: str, n_items: int) -> list[int]:
    """Indices from an LLM reply like ``"3, 0,5"``: in range, de-duplicated, in order."""
    idxs: list[int] = []
    for tok in reply.replace(" ", "").split(","):
        if tok.isdigit():
            i = int(tok)
            if i < n_items and i not in idxs:
                idxs.append(i)
    return idxs


async def llm_rerank(llm, query: str, items: list[dict], limit: int) -> list[dict]:
    """Reorder candidates by LLM-judged relevance; fused order on any failure.

    ``llm`` is a :class:`services.system_llm.SystemLLM`, read beforehand so no
    DB session needs to be held across the LLM call.
    """
    from services import llm_client

    listing = "\n".join(f"[{i}] {(it.get('content') or '')[:200]}" for i, it in enumerate(items))
    prompt = (
        "Rank these project-memory snippets by relevance to the task.\n"
        f"Task: {query}\n\nSnippets:\n{listing}\n\n"
        f"Reply with the {limit} most relevant indices as a comma-separated "
        "list, best first (e.g. '3,0,5'). Indices only, no prose."
    )
    try:
        reply = await llm_client.complete(
            vendor=llm.vendor,
            model=llm.model,
            prompt=prompt,
            max_tokens=64,
            timeout=30,
            mode=llm.mode,
        )
    except Exception:
        logger.debug("llm rerank failed; using fused order", exc_info=True)
        return items[:limit]
    idxs = parse_rerank_indices(reply.strip(), len(items))
    if not idxs:
        return items[:limit]
    ranked = [items[i] for i in idxs[:limit]]
    ranked += [it for i, it in enumerate(items) if i not in idxs][: limit - len(ranked)]
    return ranked


class RepoMemory:
    """One repo's memory knowledge base."""

    def __init__(self, session: AsyncSession, repo_id: int):
        self.session = session
        self.repo_id = repo_id

    # ── Recall ──────────────────────────────────────────────────────────
    async def recall(self, query: str, *, limit: int = 5, topic: str | None = None) -> list[dict]:
        """Hybrid recall of experience, decisions and notes (transcripts excluded).

        Settings (all optional):
            ``memory_min_similarity``     — drop weak vector hits
            ``memory_cross_repo_tunnels`` — also pull other repos' memories at
                                            half weight (off: repos stay isolated)
            ``memory_llm_rerank``         — let the system LLM reorder candidates

        Holds the session across the rerank LLM call; callers that must not
        (the runner) use :meth:`recall_candidates` + :func:`llm_rerank`.
        """
        items, rerank = await self.recall_candidates(query, limit=limit, topic=topic)
        if rerank:
            from services import system_llm

            llm = await system_llm.read(self.session)
            return await llm_rerank(llm, query, items, limit)
        return items

    async def recall_candidates(
        self, query: str, *, limit: int = 5, topic: str | None = None
    ) -> tuple[list[dict], bool]:
        """The DB half of :meth:`recall`: ``(candidates, needs_llm_rerank)``.

        When the second value is true the caller should pass the candidates to
        :func:`llm_rerank` (ideally after closing the session); otherwise the
        candidates are already the final, ``limit``-sized answer.
        """
        db = self.session
        rerank = await app_settings.get_bool_setting(db, "memory_llm_rerank", False)
        tunnels = await app_settings.get_bool_setting(db, "memory_cross_repo_tunnels", False)
        min_sim = await app_settings.get_float_setting(db, "memory_min_similarity", 0.0)
        fetch = limit * 4 if (rerank or tunnels) else limit

        items = await memory.search_memory_hybrid(
            db, self.repo_id, query, topic=topic, limit=fetch, min_similarity=min_sim
        )
        if tunnels:
            cross = await memory.search_memory_hybrid(
                db,
                None,
                query,
                topic=topic,
                exclude_repo_id=self.repo_id,
                limit=fetch,
                min_similarity=min_sim,
            )
            seen = {it["id"] for it in items}
            for it in cross:
                if it["id"] in seen:
                    continue
                it["score"] = it.get("score", 0.0) * _TUNNEL_WEIGHT
                it["cross_repo"] = True
                items.append(it)
            items.sort(key=lambda x: x.get("score", 0.0), reverse=True)

        needs_rerank = rerank and len(items) > limit
        return (items if needs_rerank else items[:limit]), needs_rerank

    async def recall_iterative(self, query: str, *, limit: int = 5) -> list[dict]:
        """LLM-planned multi-hop recall."""
        return await memory.search_memory_iterative(self.session, self.repo_id, query, limit=limit)

    async def recall_transcripts(self, query: str, *, limit: int = 5) -> list[dict]:
        """Search the verbatim transcript chunks of past runs.

        Excluded from :meth:`recall` so they do not drown out distilled
        experience; this is the on-demand "did we already try this?" lane.
        """
        return await memory.search_memory_hybrid(
            self.session, self.repo_id, query, memory_type="transcript", limit=limit
        )

    async def recall_decisions(self, query: str, *, limit: int = 5) -> list[dict]:
        """Search recorded causal decisions only."""
        return await memory.search_memory_hybrid(
            self.session, self.repo_id, query, memory_type="decision", limit=limit
        )

    def render_recall(self, memories: list[dict]) -> str:
        return memory.render_memory_recall(memories)

    # ── Write ───────────────────────────────────────────────────────────
    async def remember(
        self,
        content: str,
        *,
        kind: str = "experience",
        task_id: int | None = None,
        metadata: dict | None = None,
        topic: str | None = None,
    ) -> int:
        """Store a memory entry with an embedding."""
        return await memory.store_memory(
            self.session,
            repo_id=self.repo_id,
            content=content,
            memory_type=kind,
            task_id=task_id,
            metadata=metadata,
            topic=topic,
        )

    async def record_decision(
        self,
        *,
        problem: str,
        choice: str,
        alternatives: list[str] | None = None,
        reasoning: str = "",
        outcome: str = "",
        task_id: int | None = None,
        metadata: dict | None = None,
    ) -> int:
        return await memory.store_decision(
            self.session,
            self.repo_id,
            problem=problem,
            choice=choice,
            alternatives=alternatives,
            reasoning=reasoning,
            outcome=outcome,
            task_id=task_id,
            extra_metadata=metadata,
        )

    async def invalidate_memory(self, memory_id: int, *, superseded_by: int | None = None) -> bool:
        return await memory.invalidate_memory(self.session, memory_id, superseded_by=superseded_by)

    # ── Verbatim transcripts ────────────────────────────────────────────
    async def archive_transcript(self, task_id: int, *, task_key: str | None = None) -> int:
        """Store each run's raw transcript as embedded, de-duplicated chunks.

        Called on task success and before compaction distils the transcript
        into a lossy summary. Idempotent: chunks are keyed by a deterministic
        ``chunk_id`` (sha256 of task:run:idx), so a re-run only adds new runs.
        Returns the number of chunks stored. Never raises.
        """
        db = self.session
        try:
            runs = (
                await db.execute(
                    text(
                        "SELECT id, attempt, status, claude_output, session_id "
                        "FROM task_runs WHERE task_id = :tid AND claude_output IS NOT NULL "
                        "ORDER BY id"
                    ),
                    {"tid": task_id},
                )
            ).fetchall()
            if not runs:
                return 0
            existing = {
                r[0]
                for r in (
                    await db.execute(
                        text(
                            "SELECT metadata->>'chunk_id' FROM agent_memory "
                            "WHERE repo_id = :rid AND task_id = :tid "
                            "AND memory_type = 'transcript' AND metadata ? 'chunk_id'"
                        ),
                        {"rid": self.repo_id, "tid": task_id},
                    )
                ).fetchall()
                if r[0]
            }

            to_store: list[tuple[str, dict]] = []
            for run_id, attempt, status, output, session_id in runs:
                # Redact before chunking so a secret split across a window
                # boundary is still caught.
                chunks = chunk_text(secret_rules.redact_secrets(output)[0])
                first_kept = max(0, len(chunks) - _MAX_TRANSCRIPT_CHUNKS_PER_RUN)
                for idx, chunk in enumerate(chunks):
                    if idx < first_kept:
                        continue
                    cid = hashlib.sha256(f"{task_id}:{run_id}:{idx}".encode()).hexdigest()
                    if cid in existing:
                        continue
                    to_store.append(
                        (
                            chunk,
                            {
                                "chunk_id": cid,
                                "task_id": task_id,
                                "task_key": task_key,
                                "attempt": attempt,
                                "run_id": run_id,
                                "run_status": status,
                                "session_id": session_id,
                                "chunk_idx": idx,
                                "n_chunks": len(chunks),
                            },
                        )
                    )
            # Newest runs win when the task-wide cap bites.
            to_store = to_store[-_MAX_TRANSCRIPT_CHUNKS_PER_TASK:]
            if not to_store:
                return 0

            for i in range(0, len(to_store), _TRANSCRIPT_EMBED_BATCH):
                batch = to_store[i : i + _TRANSCRIPT_EMBED_BATCH]
                vecs = await embeddings.embed_batch([c for c, _ in batch])
                for (chunk, meta), vec in zip(batch, vecs, strict=True):
                    await db.execute(
                        text(
                            "INSERT INTO agent_memory "
                            "(repo_id, task_id, content, embedding, memory_type, metadata) "
                            "VALUES (:rid, :tid, :content, CAST(:emb AS vector), "
                            "'transcript', CAST(:meta AS JSONB))"
                        ),
                        {
                            "rid": self.repo_id,
                            "tid": task_id,
                            "content": chunk,
                            "emb": str(list(vec)) if vec is not None else None,
                            "meta": json.dumps(meta),
                        },
                    )
                await db.commit()
            await store.emit_event(
                task_id, None, "transcript_archived", {"chunks": len(to_store), "runs": len(runs)}
            )
            return len(to_store)
        except Exception:
            logger.exception("archive_transcript failed for task %s", task_id)
            await db.rollback()
            return 0

    # ── Temporal facts ──────────────────────────────────────────────────
    async def record_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        *,
        topic: str | None = None,
        confidence: float = 1.0,
        source_task_id: int | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Record a fact, superseding the current one for the same subject+predicate."""
        return await memory_facts.record_fact(
            self.session,
            self.repo_id,
            subject,
            predicate,
            obj,
            topic=topic,
            confidence=confidence,
            source_task_id=source_task_id,
            metadata=metadata,
        )

    async def invalidate_fact(self, fact_id: int, *, reason: str = "") -> bool:
        return await memory_facts.invalidate_fact(self.session, fact_id, reason=reason)

    async def query_facts(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        at_time: datetime | None = None,
        include_invalid: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        return await memory_facts.query_facts(
            self.session,
            self.repo_id,
            subject=subject,
            predicate=predicate,
            at_time=at_time,
            include_invalid=include_invalid,
            limit=limit,
        )

    async def timeline(self, subject: str, *, limit: int = 50) -> list[dict]:
        return await memory_facts.timeline(self.session, self.repo_id, subject, limit=limit)

    async def search_facts(self, query: str, *, limit: int = 8) -> list[dict]:
        return await memory_facts.search_facts(self.session, self.repo_id, query, limit=limit)

    def render_facts(self, facts: list[dict]) -> str:
        return memory_facts.render_facts(facts)

    # ── Wake-up digest ──────────────────────────────────────────────────
    async def wake_up_digest(self, *, max_age_hours: int | None = None, force: bool = False) -> str:
        """The cached digest, rebuilt when older than ``memory_digest_ttl_hours``.

        ``force=True`` always rebuilds. Returns '' when the repo has no
        knowledge to summarise yet. Never raises.
        """
        if max_age_hours is None:
            max_age_hours = await app_settings.get_int_setting(
                self.session, "memory_digest_ttl_hours", 12
            )
        cached = ""
        if not force:
            row = await self._cached_digest_row()
            if row is not None and row[0] and row[1]:
                cached = row[0]
                built_at = row[1] if row[1].tzinfo else row[1].replace(tzinfo=UTC)
                if (datetime.now(UTC) - built_at).total_seconds() < max_age_hours * 3600:
                    return cached
        digest = await self.build_wake_digest()
        if digest:
            await self.set_wake_digest(digest)
            return digest
        return cached

    async def _cached_digest_row(self):
        try:
            return (
                await self.session.execute(
                    text(
                        "SELECT wake_digest, wake_digest_at FROM repo_memory WHERE repo_id = :rid"
                    ),
                    {"rid": self.repo_id},
                )
            ).fetchone()
        except Exception:
            logger.debug("reading wake digest failed for repo %s", self.repo_id, exc_info=True)
            await self.session.rollback()
            return None

    async def get_wake_digest(self) -> str:
        """The cached digest as is, '' when none was built yet."""
        row = await self._cached_digest_row()
        return (row[0] or "") if row else ""

    async def build_wake_digest(self) -> str:
        """Assemble the digest from the KB deterministically (no LLM)."""
        db = self.session
        try:
            name_row = (
                await db.execute(
                    text("SELECT name FROM repos WHERE id = :rid"), {"rid": self.repo_id}
                )
            ).fetchone()
            stat_rows = (
                await db.execute(
                    text("SELECT status, COUNT(*) FROM tasks WHERE repo_id = :rid GROUP BY status"),
                    {"rid": self.repo_id},
                )
            ).fetchall()
            topic_rows = (
                await db.execute(
                    text(
                        "SELECT topic, COUNT(*) c FROM agent_memory "
                        "WHERE repo_id = :rid AND topic IS NOT NULL AND archived_at IS NULL "
                        "GROUP BY topic ORDER BY c DESC LIMIT 6"
                    ),
                    {"rid": self.repo_id},
                )
            ).fetchall()
            facts = await self.query_facts(limit=8)
            decision_rows = (
                await db.execute(
                    text(
                        "SELECT metadata FROM agent_memory WHERE repo_id = :rid "
                        "AND memory_type = 'decision' AND archived_at IS NULL "
                        "AND invalidated_at IS NULL ORDER BY created_at DESC LIMIT 5"
                    ),
                    {"rid": self.repo_id},
                )
            ).fetchall()
            dead_rows = (
                await db.execute(
                    text(
                        "SELECT subject, predicate, object FROM memory_facts "
                        "WHERE repo_id = :rid AND invalidated_at IS NOT NULL "
                        "ORDER BY invalidated_at DESC LIMIT 4"
                    ),
                    {"rid": self.repo_id},
                )
            ).fetchall()
        except Exception:
            logger.debug("build_wake_digest failed for repo %s", self.repo_id, exc_info=True)
            await db.rollback()
            return ""
        return render_wake_digest(
            repo_name=(name_row[0] if name_row else None) or f"repo {self.repo_id}",
            status_counts={s: int(n) for s, n in stat_rows},
            topics=[t for t, _ in topic_rows],
            facts=facts,
            decisions=[md or {} for (md,) in decision_rows],
            dead_facts=[tuple(r) for r in dead_rows],
        )

    async def set_wake_digest(self, digest: str) -> None:
        """Upsert the cached digest. Never raises."""
        try:
            await self.session.execute(
                text(
                    "INSERT INTO repo_memory (repo_id, wake_digest, wake_digest_at, updated_at) "
                    "VALUES (:rid, :d, NOW(), NOW()) "
                    "ON CONFLICT (repo_id) DO UPDATE SET "
                    "wake_digest = EXCLUDED.wake_digest, "
                    "wake_digest_at = NOW(), updated_at = NOW()"
                ),
                {"rid": self.repo_id, "d": digest},
            )
            await self.session.commit()
        except Exception:
            logger.debug("set_wake_digest failed for repo %s", self.repo_id, exc_info=True)
            await self.session.rollback()


def render_wake_digest(
    *,
    repo_name: str,
    status_counts: dict[str, int],
    topics: list[str],
    facts: list[dict],
    decisions: list[dict],
    dead_facts: list[tuple],
) -> str:
    """The digest text; '' when there is nothing beyond the repo's identity."""
    # The runner writes 'test' on success (awaiting human check); 'done' after.
    done = status_counts.get("done", 0) + status_counts.get("test", 0)
    failed = status_counts.get("failed", 0) + status_counts.get("blocked", 0)
    total = sum(status_counts.values())
    rate = f"{round(100 * done / (done + failed))}%" if (done + failed) else "n/a"
    parts = [
        "## Repo Memory Digest (wake-up)",
        f"Repo: {repo_name} — {total} tasks ({done} done, {failed} failed, {rate} success).",
    ]
    if topics:
        parts.append("Active areas: " + ", ".join(topics) + ".")
    if facts:
        parts.append("\nKnown facts:")
        parts += [f"- {f['subject']} {f['predicate']} {f['object']}" for f in facts]
    decision_lines = []
    for md in decisions:
        choice = (md.get("choice") or "").strip()
        if not choice:
            continue
        reason = (md.get("reasoning") or "").strip()
        decision_lines.append(
            ("- chose " + choice + (f" because {reason}" if reason else ""))[:200]
        )
    if decision_lines:
        parts.append("\nRecent decisions:")
        parts += decision_lines
    if dead_facts:
        parts.append("\nNo longer true (do not rely on):")
        parts += [f"- {s} {p} {o}" for s, p, o in dead_facts]
    if len(parts) <= 2:
        return ""
    return "\n".join(parts)
