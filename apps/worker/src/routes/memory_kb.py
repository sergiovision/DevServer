"""Per-repo memory knowledge base: agent lanes, console lanes and the corpus.

Three audiences share these endpoints:

- **A running task's agent** calls the task-keyed lanes
  (``/internal/tasks/{task_key}/...``) with ``curl``, using the
  ``$DEVSERVER_WORKER_URL`` / ``$DEVSERVER_TASK_KEY`` env the runner sets.
- **A coding-CLI console** (Claude Code / Codex / Gemini, through the
  ``DevServer`` MCP server) has no task: it resolves its cwd to a repo once via
  ``/repos/resolve`` and then uses the repo-scoped mirrors. Its writes are
  tagged ``source='console'`` and can be switched off with the
  ``memory_allow_console_writes`` setting.
- **The dashboard** — the repos page "Reindex" button posts to
  ``/repos/{id}/corpus/ingest``, the Ask Agent panel searches ``/corpus/search``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.base import async_session
from models.repo import Repo
from models.task import Task
from services import app_settings, corpus, repo_kb, repo_map, secret_rules, system_llm
from services.repo_kb import RepoMemory
from services.runner import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal")


# ─── Request bodies ─────────────────────────────────────────────────────────


class DecisionRequest(BaseModel):
    """A causal decision: problem → alternatives → choice → reasoning → outcome."""

    problem: str = ""
    choice: str
    alternatives: list[str] = Field(default_factory=list)
    reasoning: str = ""
    outcome: str = ""
    agent: str | None = None


class FactRequest(BaseModel):
    """A subject-predicate-object fact; supersedes the current one for subject+predicate."""

    subject: str
    predicate: str
    object: str
    topic: str | None = None
    confidence: float = 1.0
    agent: str | None = None


class InvalidateFactRequest(BaseModel):
    reason: str = ""


class RememberRequest(BaseModel):
    """A durable note for future tasks (a gotcha, a convention)."""

    content: str
    kind: Literal["note", "experience", "gotcha"] = "note"
    topic: str | None = None
    agent: str | None = None


class CorpusIngestRequest(BaseModel):
    kind: str = "code"  # 'code' | 'doc'
    full: bool = False


# ─── Shared helpers ─────────────────────────────────────────────────────────


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


async def _task_or_404(db: AsyncSession, task_key: str) -> Task:
    task = (
        await db.execute(
            select(Task).where(Task.task_key == task_key).order_by(Task.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, f"task {task_key!r} not found")
    return task


async def _repo_or_404(db: AsyncSession, repo_id: int) -> Repo:
    repo = await db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(404, f"repo {repo_id} not found")
    return repo


def _require_repo(task: Task) -> int:
    if task.repo_id is None:
        raise HTTPException(400, "task has no repo — memory is repo-scoped")
    return task.repo_id


async def _require_console_writes(db: AsyncSession) -> None:
    """403 when console writes are switched off (``memory_allow_console_writes``)."""
    if not await app_settings.get_bool_setting(db, "memory_allow_console_writes", True):
        raise HTTPException(
            403, "console memory writes are disabled (memory_allow_console_writes=false)"
        )


def _repo_root(repo: Repo) -> str | None:
    return corpus.repo_root(repo.provider, repo.gitea_url, repo.name)


def _path_matches(requested: str, root: str | None) -> bool:
    """True when ``requested`` is ``root`` or lies under it."""
    if not root:
        return False
    req, base = os.path.realpath(requested), os.path.realpath(root)
    return req == base or req.startswith(base + os.sep)


def _norm_remote(url: str | None) -> str:
    return (url or "").strip().rstrip("/").removesuffix(".git").lower()


async def _emit_memory_queried(task_id: int, lane: str, count: int) -> None:
    """Best-effort ``memory_queried`` event so the dashboard shows lane usage."""
    try:
        await store.emit_event(task_id, None, "memory_queried", {"lane": lane, "count": count})
    except Exception:
        logger.debug("memory_queried event failed (task %s)", task_id, exc_info=True)


def _memory_hits(hits: list[dict]) -> dict:
    return {
        "count": len(hits),
        "memories": [
            {
                "id": h.get("id"),
                "content": h.get("content"),
                "kind": h.get("memory_type"),
                "similarity": round(h.get("similarity", 0.0), 3),
                "cross_repo": h.get("cross_repo", False),
                "created_at": h.get("created_at"),
            }
            for h in hits
        ],
    }


def _transcript_hits(hits: list[dict]) -> dict:
    return {
        "count": len(hits),
        "memories": [
            {
                "content": h.get("content"),
                "similarity": round(h.get("similarity", 0.0), 3),
                "metadata": h.get("metadata"),
                "created_at": h.get("created_at"),
            }
            for h in hits
        ],
    }


def _decision_hits(hits: list[dict]) -> dict:
    return {
        "count": len(hits),
        "decisions": [
            {
                "id": h.get("id"),
                "content": h.get("content"),
                "metadata": h.get("metadata"),
                "similarity": round(h.get("similarity", 0.0), 3),
                "created_at": h.get("created_at"),
            }
            for h in hits
        ],
    }


async def _facts_lane(kb: RepoMemory, q: str | None, subject: str | None, limit: int) -> list[dict]:
    if q:
        return await kb.search_facts(q, limit=limit)
    return await kb.query_facts(subject=subject, limit=limit)


async def _recall(repo_id: int, q: str, *, limit: int, topic: str | None) -> list[dict]:
    """``RepoMemory.recall`` without holding a DB session across the rerank LLM call."""
    async with async_session() as db:
        hits, rerank = await RepoMemory(db, repo_id).recall_candidates(q, limit=limit, topic=topic)
        llm = await system_llm.read(db) if rerank else None
    if rerank:
        hits = await repo_kb.llm_rerank(llm, q, hits, limit)
    return hits


async def _repo_map(root: str | None, subdir: str | None, max_chars: int) -> dict:
    if not root or not os.path.isdir(root):
        return {"ok": False, "map": "", "stats": {}, "error": "repo root not available"}
    # A full tree walk + parse: keep it off the event loop.
    rendered, stats = await asyncio.to_thread(
        repo_map.build_repo_map, root, max_chars=_clamp(max_chars, 1000, 20000), subdir=subdir
    )
    return {"ok": True, "map": rendered, "stats": stats}


# ─── Task-keyed lanes (called by a running agent via curl) ──────────────────


@router.post("/tasks/{task_key}/decisions")
async def task_record_decision(task_key: str, body: DecisionRequest):
    """Record a non-obvious design choice so future tasks recall the reasoning."""
    if not body.choice.strip():
        raise HTTPException(400, "choice cannot be empty")
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        repo_id = _require_repo(task)
        meta: dict = {"task_key": task_key, "source": "agent"}
        if body.agent:
            meta["agent"] = body.agent
        mem_id = await RepoMemory(db, repo_id).record_decision(
            problem=body.problem,
            choice=body.choice,
            alternatives=body.alternatives,
            reasoning=body.reasoning,
            outcome=body.outcome,
            task_id=task.id,
            metadata=meta,
        )
    choice = secret_rules.redact_secrets(body.choice)[0][:120]
    try:
        await store.emit_event(
            task.id, None, "decision_recorded", {"memory_id": mem_id, "choice": choice}
        )
    except Exception:
        logger.debug("decision_recorded event failed (task %s)", task_key, exc_info=True)
    return {"ok": True, "memory_id": mem_id}


@router.post("/tasks/{task_key}/facts")
async def task_record_fact(task_key: str, body: FactRequest):
    """Record a durable fact; a new one for the same subject+predicate supersedes the old."""
    if not body.subject.strip() or not body.predicate.strip():
        raise HTTPException(400, "subject and predicate are required")
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        repo_id = _require_repo(task)
        fact_id = await RepoMemory(db, repo_id).record_fact(
            body.subject,
            body.predicate,
            body.object,
            topic=body.topic,
            confidence=body.confidence,
            source_task_id=task.id,
            metadata={"source": "agent", **({"agent": body.agent} if body.agent else {})},
        )
    return {"ok": bool(fact_id), "fact_id": fact_id}


@router.get("/tasks/{task_key}/memory/recall")
async def task_recall(task_key: str, q: str, topic: str | None = None, limit: int = 5):
    """What past tasks learned in this repo (hybrid recall, transcripts excluded)."""
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        if task.repo_id is None:
            return _memory_hits([])
        task_id, repo_id = task.id, task.repo_id
    hits = await _recall(repo_id, q, limit=_clamp(limit, 1, 20), topic=topic)
    await _emit_memory_queried(task_id, "recall", len(hits))
    return _memory_hits(hits)


@router.post("/tasks/{task_key}/memory/remember")
async def task_remember(task_key: str, body: RememberRequest):
    """Persist a durable note for future tasks."""
    if not body.content.strip():
        raise HTTPException(400, "content cannot be empty")
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        repo_id = _require_repo(task)
        meta: dict = {"task_key": task_key, "source": "agent"}
        if body.agent:
            meta["agent"] = body.agent
        mem_id = await RepoMemory(db, repo_id).remember(
            body.content, kind=body.kind, task_id=task.id, topic=body.topic, metadata=meta
        )
    return {"ok": bool(mem_id), "memory_id": mem_id}


@router.get("/tasks/{task_key}/memory/transcripts")
async def task_transcripts(task_key: str, q: str, limit: int = 5):
    """Search verbatim transcripts of past runs — "did we already try this?"."""
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        if task.repo_id is None:
            return _transcript_hits([])
        hits = await RepoMemory(db, task.repo_id).recall_transcripts(q, limit=_clamp(limit, 1, 20))
        await _emit_memory_queried(task.id, "transcripts", len(hits))
    return _transcript_hits(hits)


@router.get("/tasks/{task_key}/memory/facts")
async def task_facts(
    task_key: str, q: str | None = None, subject: str | None = None, limit: int = 10
):
    """Current project facts: semantic search with ``q``, else filtered by ``subject``."""
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        if task.repo_id is None:
            return {"count": 0, "facts": []}
        facts = await _facts_lane(RepoMemory(db, task.repo_id), q, subject, _clamp(limit, 1, 50))
        await _emit_memory_queried(task.id, "facts", len(facts))
    return {"count": len(facts), "facts": facts}


@router.get("/tasks/{task_key}/memory/decisions")
async def task_decisions(task_key: str, q: str, limit: int = 5):
    """Past design decisions and their reasoning."""
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        if task.repo_id is None:
            return _decision_hits([])
        hits = await RepoMemory(db, task.repo_id).recall_decisions(q, limit=_clamp(limit, 1, 20))
        await _emit_memory_queried(task.id, "decisions", len(hits))
    return _decision_hits(hits)


@router.get("/tasks/{task_key}/repo-map")
async def task_repo_map(task_key: str, subdir: str | None = None, max_chars: int = 8000):
    """A deeper symbol map of one sub-directory than the snapshot in the prompt."""
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        if task.repo_id is None:
            return {"ok": False, "map": "", "stats": {}, "error": "task has no repo"}
        repo = await _repo_or_404(db, task.repo_id)
        task_id, root = task.id, _repo_root(repo)
    result = await _repo_map(root, subdir, max_chars)
    await _emit_memory_queried(task_id, "repo_map", result["stats"].get("files", 0))
    return result


@router.get("/tasks/{task_key}/corpus/search")
async def task_corpus_search(task_key: str, q: str, kind: str | None = None, limit: int = 8):
    """Semantic search over the task repo's indexed code (``kind=code``) or docs."""
    async with async_session() as db:
        task = await _task_or_404(db, task_key)
        if task.repo_id is None:
            return {"count": 0, "results": [], "error": "task has no repo"}
        hits = await corpus.search(db, task.repo_id, q, kind=kind, limit=_clamp(limit, 1, 25))
        await _emit_memory_queried(task.id, "corpus", len(hits))
    return {"count": len(hits), "results": hits}


@router.post("/facts/{fact_id}/invalidate")
async def invalidate_fact(fact_id: int, body: InvalidateFactRequest):
    """Mark a fact no longer true."""
    async with async_session() as db:
        # Invalidation is by id; the repo id is irrelevant here.
        ok = await RepoMemory(db, 0).invalidate_fact(fact_id, reason=body.reason)
    if not ok:
        raise HTTPException(404, f"fact {fact_id} not found or already invalidated")
    return {"ok": True, "fact_id": fact_id}


# ─── Repo-scoped lanes (console / MCP server) ───────────────────────────────


@router.get("/repos/resolve")
async def resolve_repo(path: str | None = None, remote: str | None = None):
    """Map a console's cwd (or a git remote URL) to a DevServer repo.

    Filesystem containment wins (deepest root first), then the normalised
    clone URL. 404 means "this folder is not a DevServer repo".
    """
    if not path and not remote:
        raise HTTPException(400, "path or remote is required")
    async with async_session() as db:
        repos = (await db.execute(select(Repo).where(Repo.active.is_(True)))).scalars().all()

    if path:
        best: tuple[int, Repo] | None = None
        for repo in repos:
            root = _repo_root(repo)
            if root and _path_matches(path, root):
                depth = len(os.path.realpath(root))
                if best is None or depth > best[0]:
                    best = (depth, repo)
        if best is not None:
            repo = best[1]
            return {"repo_id": repo.id, "name": repo.name, "provider": repo.provider or "gitea"}

    want = _norm_remote(remote)
    if want:
        for repo in repos:
            if _norm_remote(repo.clone_url) == want:
                return {"repo_id": repo.id, "name": repo.name, "provider": repo.provider or "gitea"}

    raise HTTPException(404, "no DevServer repo matches this path/remote")


@router.get("/repos/{repo_id}/wake-digest")
async def repo_wake_digest(repo_id: int, force: bool = False):
    """Small grounding digest: repo identity, current facts, recent decisions."""
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        digest = await RepoMemory(db, repo_id).wake_up_digest(force=force)
    return {"repo_id": repo_id, "digest": digest}


@router.get("/repos/{repo_id}/memory/recall")
async def repo_recall(repo_id: int, q: str, topic: str | None = None, limit: int = 5):
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
    hits = await _recall(repo_id, q, limit=_clamp(limit, 1, 20), topic=topic)
    return _memory_hits(hits)


@router.get("/repos/{repo_id}/memory/transcripts")
async def repo_transcripts(repo_id: int, q: str, limit: int = 5):
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        hits = await RepoMemory(db, repo_id).recall_transcripts(q, limit=_clamp(limit, 1, 20))
    return _transcript_hits(hits)


@router.get("/repos/{repo_id}/memory/decisions")
async def repo_decisions(repo_id: int, q: str, limit: int = 5):
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        hits = await RepoMemory(db, repo_id).recall_decisions(q, limit=_clamp(limit, 1, 20))
    return _decision_hits(hits)


@router.get("/repos/{repo_id}/memory/facts")
async def repo_facts_lane(
    repo_id: int, q: str | None = None, subject: str | None = None, limit: int = 10
):
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        facts = await _facts_lane(RepoMemory(db, repo_id), q, subject, _clamp(limit, 1, 50))
    return {"count": len(facts), "facts": facts}


@router.get("/repos/{repo_id}/facts")
async def list_facts(
    repo_id: int, subject: str | None = None, include_invalid: bool = False, limit: int = 50
):
    """A repo's facts — current only unless ``include_invalid``."""
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        facts = await RepoMemory(db, repo_id).query_facts(
            subject=subject, include_invalid=include_invalid, limit=_clamp(limit, 1, 500)
        )
    return {"count": len(facts), "facts": facts}


@router.get("/repos/{repo_id}/facts/timeline")
async def fact_timeline(repo_id: int, subject: str, limit: int = 50):
    """Full chronological history of one subject's facts."""
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        facts = await RepoMemory(db, repo_id).timeline(subject, limit=_clamp(limit, 1, 500))
    return {"count": len(facts), "facts": facts}


@router.get("/repos/{repo_id}/repo-map")
async def repo_repo_map(repo_id: int, subdir: str | None = None, max_chars: int = 8000):
    async with async_session() as db:
        repo = await _repo_or_404(db, repo_id)
    return await _repo_map(_repo_root(repo), subdir, max_chars)


@router.post("/repos/{repo_id}/memory/remember")
async def repo_remember(repo_id: int, body: RememberRequest):
    """A durable note written from a console (``source='console'``)."""
    if not body.content.strip():
        raise HTTPException(400, "content cannot be empty")
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        await _require_console_writes(db)
        meta: dict = {"source": "console"}
        if body.agent:
            meta["agent"] = body.agent
        mem_id = await RepoMemory(db, repo_id).remember(
            body.content, kind=body.kind, topic=body.topic, metadata=meta
        )
    return {"ok": bool(mem_id), "memory_id": mem_id}


@router.post("/repos/{repo_id}/facts")
async def repo_record_fact(repo_id: int, body: FactRequest):
    if not body.subject.strip() or not body.predicate.strip():
        raise HTTPException(400, "subject and predicate are required")
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        await _require_console_writes(db)
        fact_id = await RepoMemory(db, repo_id).record_fact(
            body.subject,
            body.predicate,
            body.object,
            topic=body.topic,
            confidence=body.confidence,
            metadata={"source": "console", **({"agent": body.agent} if body.agent else {})},
        )
    return {"ok": bool(fact_id), "fact_id": fact_id}


@router.post("/repos/{repo_id}/decisions")
async def repo_record_decision(repo_id: int, body: DecisionRequest):
    if not body.choice.strip():
        raise HTTPException(400, "choice cannot be empty")
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        await _require_console_writes(db)
        meta: dict = {"source": "console"}
        if body.agent:
            meta["agent"] = body.agent
        mem_id = await RepoMemory(db, repo_id).record_decision(
            problem=body.problem,
            choice=body.choice,
            alternatives=body.alternatives,
            reasoning=body.reasoning,
            outcome=body.outcome,
            metadata=meta,
        )
    return {"ok": True, "memory_id": mem_id}


# ─── Code/doc corpus ────────────────────────────────────────────────────────


@router.get("/repos/{repo_id}/corpus/search")
async def repo_corpus_search(
    repo_id: int,
    q: str,
    kind: str | None = None,
    language: str | None = None,
    path_prefix: str | None = None,
    limit: int = 8,
):
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        hits = await corpus.search(
            db, repo_id, q, kind=kind, language=language, path_prefix=path_prefix, limit=limit
        )
    return {"count": len(hits), "results": hits}


@router.get("/corpus/search")
async def corpus_search_all(
    q: str,
    kind: str | None = None,
    language: str | None = None,
    repo_ids: str | None = None,
    limit: int = 8,
):
    """Search every active repo (or a comma-separated ``repo_ids`` subset) at once."""
    ids: list[int] | None = None
    if repo_ids:
        try:
            ids = [int(p) for p in repo_ids.split(",") if p.strip()]
        except ValueError:
            raise HTTPException(400, "repo_ids must be comma-separated integers") from None
    async with async_session() as db:
        hits = await corpus.search_repos(db, ids, q, kind=kind, language=language, limit=limit)
    return {"count": len(hits), "results": hits}


@router.get("/repos/{repo_id}/corpus/stats")
async def repo_corpus_stats(repo_id: int):
    """Per-kind chunk/file counts, last reindex time and any running job."""
    async with async_session() as db:
        await _repo_or_404(db, repo_id)
        return await corpus.stats(db, repo_id)


@router.post("/repos/{repo_id}/corpus/ingest")
async def repo_corpus_ingest(repo_id: int, body: CorpusIngestRequest):
    """(Re)index the code or doc corpus in the background; poll ``corpus/stats``."""
    kind = "doc" if str(body.kind).lower().startswith("doc") else "code"
    async with async_session() as db:
        repo = await _repo_or_404(db, repo_id)
    root = _repo_root(repo)
    if not root or not os.path.isdir(root):
        raise HTTPException(400, "repo root not available on this worker — run Refresh Git first")
    if not corpus.start_ingest(repo_id, root, kind, full=body.full):
        return {"ok": True, "kind": kind, "status": "already running"}
    return {
        "ok": True,
        "kind": kind,
        "status": "started",
        "note": "indexing in the background; call corpus_stats to monitor",
    }
