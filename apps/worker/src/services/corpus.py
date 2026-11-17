"""Per-repo code/doc corpus — semantic search over a repo's source & docs.

Powers the ``code_search`` / ``doc_search`` / ``corpus_stats`` /
``corpus_ingest`` lanes of the ``DevServer`` MCP server, the agent's
``corpus/search`` lane and the repos page "Reindex" button, all reached
through ``routes/memory_kb.py``.

Storage is the ``corpus_chunks`` table: one row per overlapping text window of
an indexed file, with a 768-dim fastembed vector and a generated tsvector for a
lexical fallback. Chunks are content-addressed by ``file_sha`` so an incremental
re-ingest only re-embeds files whose contents changed; deleted files are pruned.

Ingest is potentially slow (CPU embedding), so the HTTP route launches it as a
background job — ``run_ingest_job`` owns its own DB session and ``is_indexing``
lets ``corpus_stats`` report progress. The public functions never raise: each
degrades to an empty / ``ok=False`` result so the MCP layer reports cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services import app_settings, embeddings, git_ops, repo_map, secret_rules

logger = logging.getLogger(__name__)

# ── File selection ───────────────────────────────────────────────────────────
# Extension → full language name. Search-friendly (the MCP passes "python",
# "typescript", "csharp", …) so we store the long form, not repo_map's "ts"/"js".
CODE_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".css": "css",
    ".scss": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}

DOC_EXT = {".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc"}
DOC_FILENAMES = {"README", "CHANGELOG", "LICENSE", "AGENTS", "CLAUDE"}

# Search-filter aliases so `language="ts"` matches stored `typescript`.
_LANG_ALIASES = {
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "py": "python",
    "cs": "csharp",
    "c#": "csharp",
    "golang": "go",
    "rb": "ruby",
    "md": "markdown",
}

# Budgets — keep a single ingest bounded so a huge repo can't run away.
_MAX_FILE_BYTES = 400_000  # skip larger files (generated/minified/vendored)
_MAX_FILES = 4_000
_MAX_CHUNKS_PER_FILE = 200
_MAX_CHUNKS_PER_INGEST = 12_000
_EMBED_BATCH = 64

# Chunking — overlapping char windows (mirrors repo_kb's transcript chunker).
_CHUNK_CHARS = 1_500
_CHUNK_OVERLAP = 200

# Repo ids with an ingest job currently running, keyed by (repo_id, kind).
_active: set[tuple[int, str]] = set()
# Strong references to background ingest tasks so they are not GC'd mid-run.
_jobs: set[asyncio.Task] = set()


def _norm_kind(kind: str | None) -> str:
    return "doc" if str(kind or "").lower().startswith("doc") else "code"


def _chunk_text(s: str) -> list[str]:
    """Split text into overlapping char windows. Empty input → []."""
    s = s or ""
    if not s.strip():
        return []
    out: list[str] = []
    start, n = 0, len(s)
    while start < n:
        end = min(start + _CHUNK_CHARS, n)
        out.append(s[start:end])
        if end >= n:
            break
        start = end - _CHUNK_OVERLAP
        if len(out) >= _MAX_CHUNKS_PER_FILE:
            break
    return out


def _read(full_path: str) -> str | None:
    try:
        with open(full_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _classify(filename: str, kind: str) -> str | None:
    """Return the language for a file when it belongs in ``kind``'s corpus, else None."""
    ext = os.path.splitext(filename)[1].lower()
    if kind == "code":
        return CODE_EXT_LANG.get(ext)
    if ext in DOC_EXT:
        return "markdown" if ext in {".md", ".mdx", ".markdown"} else "text"
    if os.path.splitext(filename)[0].upper() in DOC_FILENAMES:
        return "text"
    return None


def _iter_files(root: str, kind: str):
    """Yield ``(full_path, rel_path, language)`` for indexable files under ``root``.

    Reuses ``repo_map``'s directory-skip rules so vendored/build/cache trees are
    never walked. Symlinks are skipped and every file must resolve inside the
    repo root, so a link can never pull an outside file (``~/.ssh``, ``/etc``)
    into the index.
    """
    real_root = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not repo_map._should_skip_dir(d)]
        for filename in filenames:
            lang = _classify(filename, kind)
            if not lang:
                continue
            full_path = os.path.join(dirpath, filename)
            if os.path.islink(full_path):
                continue
            real = os.path.realpath(full_path)
            if not real.startswith(real_root + os.sep):
                continue
            try:
                size = os.path.getsize(full_path)
            except OSError:
                continue
            if size == 0 or size > _MAX_FILE_BYTES:
                continue
            # Store repo-relative paths with forward slashes so search filters
            # (path_prefix) and cross-references stay consistent on every OS.
            yield full_path, os.path.relpath(full_path, root).replace(os.sep, "/"), lang


@dataclass
class _Scan:
    """A snapshot of the files to (re)index, taken without touching the DB."""

    on_disk: set[str] = field(default_factory=set)
    changed_paths: list[str] = field(default_factory=list)
    # path, language, sha, chunk index, (redacted) chunk text
    pending: list[tuple[str, str, str, int, str]] = field(default_factory=list)
    stale: set[str] = field(default_factory=set)
    truncated: bool = False


def _scan(root: str, kind: str, existing: dict[str, str | None]) -> _Scan:
    """Walk ``root`` and read every changed file. Blocking — run it in a thread.

    ``existing`` maps already-indexed paths to their ``file_sha``; unchanged
    files are skipped. Chunk text is secret-redacted before it is stored (the
    sha is taken over the raw content so change detection stays exact).
    """
    scan = _Scan()
    for full_path, rel, lang in _iter_files(root, kind):
        if len(scan.on_disk) >= _MAX_FILES:
            scan.truncated = True
            break
        content = _read(full_path)
        if content is None or "\x00" in content[:1024]:
            continue
        scan.on_disk.add(rel)
        sha = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        if existing.get(rel) == sha:
            continue  # unchanged since last ingest
        scan.changed_paths.append(rel)
        redacted, _ = secret_rules.redact_secrets(content)
        for idx, chunk in enumerate(_chunk_text(redacted)):
            scan.pending.append((rel, lang, sha, idx, chunk))
        if len(scan.pending) >= _MAX_CHUNKS_PER_INGEST:
            scan.truncated = True
            break

    # Files that disappeared since the last ingest. A truncated walk never
    # reached some files, so only prune paths that are really gone from disk.
    missing = set(existing) - scan.on_disk
    if scan.truncated:
        missing = {p for p in missing if not os.path.lexists(os.path.join(root, p))}
    scan.stale = missing
    return scan


async def _existing_shas(session: AsyncSession, repo_id: int, kind: str) -> dict[str, str | None]:
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT path, file_sha FROM corpus_chunks WHERE repo_id = :r AND kind = :k"
            ),
            {"r": repo_id, "k": kind},
        )
    ).fetchall()
    return dict(rows)


async def _apply(session: AsyncSession, repo_id: int, kind: str, scan: _Scan) -> dict:
    """Prune stale/changed paths, then embed + insert the scan's chunks."""
    # Prune chunks for changed files (so re-index is clean) and for files
    # that disappeared from disk since the last incremental ingest.
    for path in set(scan.changed_paths) | scan.stale:
        await session.execute(
            text("DELETE FROM corpus_chunks WHERE repo_id = :r AND kind = :k AND path = :p"),
            {"r": repo_id, "k": kind, "p": path},
        )
    await session.commit()

    # Embed + insert in batches, committing each batch so a long job shows
    # incremental progress in corpus_stats and survives interruption.
    written = 0
    for i in range(0, len(scan.pending), _EMBED_BATCH):
        batch = scan.pending[i : i + _EMBED_BATCH]
        vecs = await embeddings.embed_batch([c for *_, c in batch])
        for (rel, lang, sha, idx, chunk), vec in zip(batch, vecs, strict=True):
            await session.execute(
                text(
                    "INSERT INTO corpus_chunks "
                    "(repo_id, kind, path, language, chunk_idx, content, "
                    " file_sha, embedding, updated_at) "
                    "VALUES (:r, :k, :p, :lang, :idx, :content, :sha, "
                    "        CAST(:emb AS vector), NOW()) "
                    "ON CONFLICT (repo_id, kind, path, chunk_idx) DO UPDATE SET "
                    "content = EXCLUDED.content, language = EXCLUDED.language, "
                    "file_sha = EXCLUDED.file_sha, embedding = EXCLUDED.embedding, "
                    "updated_at = NOW()"
                ),
                {
                    "r": repo_id,
                    "k": kind,
                    "p": rel,
                    "lang": lang,
                    "idx": idx,
                    "content": chunk,
                    "sha": sha,
                    "emb": (str(list(vec)) if vec is not None else None),
                },
            )
            written += 1
        await session.commit()

    return {
        "ok": True,
        "kind": kind,
        "files_indexed": len(scan.changed_paths),
        "files_on_disk": len(scan.on_disk),
        "files_pruned": len(scan.stale),
        "chunks_written": written,
        "truncated": scan.truncated,
    }


# ── Ingest ───────────────────────────────────────────────────────────────────
async def ingest(
    session: AsyncSession,
    repo_id: int,
    root: str | None,
    *,
    kind: str = "code",
    full: bool = False,
    scan: _Scan | None = None,
) -> dict:
    """(Re)index a repo's ``code`` or ``doc`` corpus. Incremental by default.

    Walks ``root``, content-addresses each file, and only re-embeds files whose
    sha changed since the last ingest (``full=True`` rebuilds from scratch).
    Files removed from disk are pruned. A pre-taken ``scan`` (see
    :func:`auto_index`) skips the walk. Returns a stats dict; never raises.
    """
    kind = _norm_kind(kind)
    if scan is None and (not root or not os.path.isdir(root)):
        return {"ok": False, "kind": kind, "error": "repo root not available"}

    try:
        if scan is None:
            if full:
                await session.execute(
                    text("DELETE FROM corpus_chunks WHERE repo_id = :r AND kind = :k"),
                    {"r": repo_id, "k": kind},
                )
                await session.commit()
                existing: dict[str, str | None] = {}
            else:
                existing = await _existing_shas(session, repo_id, kind)
            assert root is not None
            # The walk + file reads are blocking I/O; keep them off the event loop.
            scan = await asyncio.to_thread(_scan, root, kind, existing)
        return await _apply(session, repo_id, kind, scan)
    except Exception:
        logger.exception("corpus ingest failed (repo=%s kind=%s)", repo_id, kind)
        with contextlib.suppress(Exception):
            await session.rollback()
        return {"ok": False, "kind": kind, "error": "ingest failed"}


# ── Search ───────────────────────────────────────────────────────────────────
async def search(
    session: AsyncSession,
    repo_id: int,
    query: str,
    *,
    kind: str | None = None,
    language: str | None = None,
    path_prefix: str | None = None,
    limit: int = 8,
) -> list[dict]:
    """Semantic search over the corpus (vector cosine; lexical fallback).

    ``kind`` ('code'|'doc'), ``language`` and ``path_prefix`` narrow the scan.
    Returns ranked chunk hits with a trimmed snippet. Never raises.
    """
    limit = max(1, min(int(limit or 8), 30))
    if not query or not query.strip():
        return []

    filters = ["repo_id = :r"]
    params: dict[str, object] = {"r": repo_id, "lim": limit}
    if kind:
        filters.append("kind = :k")
        params["k"] = _norm_kind(kind)
    if language:
        lang = language.strip().lower()
        params["lang"] = _LANG_ALIASES.get(lang, lang)
        filters.append("LOWER(language) = :lang")
    if path_prefix:
        prefix = path_prefix.strip().replace("\\", "/")
        while prefix.startswith("./"):
            prefix = prefix.removeprefix("./")
        prefix = prefix.rstrip("/")
        # Escape LIKE wildcards so `_` / `%` in a real path match literally.
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["pp"] = escaped + "%"
        filters.append("path LIKE :pp ESCAPE '\\'")
    where = " AND ".join(filters)

    try:
        emb = await embeddings.embed(query)
        if emb is not None:
            params["emb"] = str(list(emb))
            sql = text(
                f"SELECT path, language, kind, chunk_idx, content, "
                f"       1 - (embedding <=> CAST(:emb AS vector)) AS similarity "
                f"FROM corpus_chunks "
                f"WHERE {where} AND embedding IS NOT NULL "
                f"ORDER BY embedding <=> CAST(:emb AS vector) "
                f"LIMIT :lim"
            )
            rows = (await session.execute(sql, params)).fetchall()
            return [_hit(r, float(r[5])) for r in rows]

        # No embedding model available → lexical full-text fallback.
        params["q"] = query
        sql = text(
            f"SELECT path, language, kind, chunk_idx, content, "
            f"       ts_rank(content_tsv, plainto_tsquery('english', :q)) AS rank "
            f"FROM corpus_chunks "
            f"WHERE {where} AND content_tsv @@ plainto_tsquery('english', :q) "
            f"ORDER BY rank DESC LIMIT :lim"
        )
        rows = (await session.execute(sql, params)).fetchall()
        return [_hit(r, float(r[5])) for r in rows]
    except Exception:
        logger.exception("corpus search failed (repo=%s)", repo_id)
        return []


async def search_repos(
    session: AsyncSession,
    repo_ids: list[int] | None,
    query: str,
    *,
    kind: str | None = None,
    language: str | None = None,
    limit: int = 8,
    per_repo_limit: int = 6,
) -> list[dict]:
    """Search several repositories at once, merged by score.

    :func:`search` is unconditionally scoped to a single repo, which is right
    for an agent working inside one checkout. The Ask Agent panel has the
    opposite problem: the operator asks "where do we do X?" without knowing, or
    caring, which repo it lives in.

    ``repo_ids=None`` searches every active repo. Each hit is tagged with its
    ``repo_id`` / ``repo_name`` so the answer can say where it came from.
    Fan-out is sequential and bounded — a per-repo scan is sub-millisecond and
    running them concurrently on one session would need a session each.
    Never raises, matching :func:`search`.
    """
    limit = max(1, min(int(limit or 8), 30))
    if not query or not query.strip():
        return []

    try:
        if repo_ids:
            rows = (
                await session.execute(
                    text("SELECT id, name FROM repos WHERE id = ANY(:ids)"),
                    {"ids": list(repo_ids)},
                )
            ).fetchall()
        else:
            rows = (
                await session.execute(text("SELECT id, name FROM repos WHERE active ORDER BY id"))
            ).fetchall()
    except Exception:
        logger.exception("corpus cross-repo search: repo lookup failed")
        return []

    merged: list[dict] = []
    for repo_id, repo_name in rows:
        hits = await search(
            session,
            repo_id,
            query,
            kind=kind,
            language=language,
            limit=per_repo_limit,
        )
        for hit in hits:
            hit["repo_id"] = repo_id
            hit["repo_name"] = repo_name
            merged.append(hit)

    merged.sort(key=lambda h: h.get("score") or 0.0, reverse=True)
    return merged[:limit]


def _hit(row, score: float) -> dict:
    return {
        "path": row[0],
        "language": row[1],
        "kind": row[2],
        "chunk": row[3],
        "score": round(score, 3),
        "snippet": (row[4] or "")[:700],
    }


# ── Stats ────────────────────────────────────────────────────────────────────
async def stats(session: AsyncSession, repo_id: int) -> dict:
    """Per-kind chunk/file counts + last reindex time, plus any active job."""
    empty = {"chunks": 0, "files": 0, "last_indexed": None}
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT kind, COUNT(*), COUNT(DISTINCT path), MAX(updated_at) "
                    "FROM corpus_chunks WHERE repo_id = :r GROUP BY kind"
                ),
                {"r": repo_id},
            )
        ).fetchall()
        by_kind = {
            kind: {"chunks": int(c), "files": int(f), "last_indexed": str(last) if last else None}
            for kind, c, f, last in rows
        }
    except Exception:
        logger.exception("corpus stats failed (repo=%s)", repo_id)
        by_kind = {}
    return {
        "repo_id": repo_id,
        "code": by_kind.get("code", dict(empty)),
        "doc": by_kind.get("doc", dict(empty)),
        "indexing": is_indexing(repo_id),
    }


# ── Background job orchestration ──────────────────────────────────────────────
def is_indexing(repo_id: int) -> list[str]:
    """Kinds currently being (re)indexed for this repo, e.g. ``["code"]``."""
    return [k for (r, k) in _active if r == repo_id]


async def run_ingest_job(
    repo_id: int, root: str, kind: str, full: bool, scan: _Scan | None = None
) -> None:
    """Run an ingest on its own DB session, tracked in ``_active``. Never raises.

    Launched as a background task by the HTTP route so the MCP call returns
    immediately instead of blocking on CPU embedding.
    """
    kind = _norm_kind(kind)
    key = (repo_id, kind)
    if key in _active:
        return
    _active.add(key)
    try:
        from models.base import async_session  # deferred — avoid import cycle

        async with async_session() as session:
            res = await ingest(session, repo_id, root, kind=kind, full=full, scan=scan)
        logger.info("corpus ingest done (repo=%s kind=%s): %s", repo_id, kind, res)
    except Exception:
        logger.exception("corpus ingest job crashed (repo=%s kind=%s)", repo_id, kind)
    finally:
        _active.discard(key)


def repo_root(provider: str | None, local_root: str | None, name: str) -> str | None:
    """Filesystem root a repo is indexed from: its local folder, else its worktree.

    The search lane, the ingest route and the post-task auto-index all resolve
    the root through here so the index stays consistent.
    """
    if git_ops.is_local_provider(provider):
        try:
            return git_ops.resolve_local_root(local_root)
        except RuntimeError:
            return None
    return git_ops.get_worktree_path(name)


def start_ingest(
    repo_id: int, root: str, kind: str, *, full: bool = False, scan: _Scan | None = None
) -> bool:
    """Launch a background ingest; False when one is already running for that kind."""
    kind = _norm_kind(kind)
    if kind in is_indexing(repo_id):
        return False
    job = asyncio.create_task(
        run_ingest_job(repo_id, root, kind, full, scan), name=f"corpus-ingest-{repo_id}-{kind}"
    )
    _jobs.add(job)
    job.add_done_callback(_jobs.discard)
    return True


async def auto_index(
    repo_id: int,
    *,
    provider: str | None,
    local_root: str | None,
    name: str,
) -> list[str]:
    """Refresh both corpora after a task succeeds. Returns the kinds started.

    Opt-in via the ``corpus_auto_index`` setting (default off). Called by the
    runner's ``finalise`` *after* the checkout is back on the default branch
    (or the operator's branch, for local repos) and while the repo lock is
    still held: the file walk/read happens here, synchronously, so the index
    reflects that tree and not the next task's ``agent/*`` branch. Only the
    slow embedding step runs in the background, from the in-memory snapshot.
    Incremental (content-addressed by ``file_sha``). Never raises.
    """
    from models.base import async_session  # deferred — avoid import cycle

    try:
        async with async_session() as session:
            if not await app_settings.get_bool_setting(session, "corpus_auto_index", False):
                return []
            root = repo_root(provider, local_root, name)
            if not root or not os.path.isdir(root):
                return []
            kinds = [k for k in ("code", "doc") if k not in is_indexing(repo_id)]
            existing = {k: await _existing_shas(session, repo_id, k) for k in kinds}
        started = []
        for kind in kinds:
            scan = await asyncio.to_thread(_scan, root, kind, existing[kind])
            if start_ingest(repo_id, root, kind, scan=scan):
                started.append(kind)
        return started
    except Exception:
        logger.debug("corpus auto-index failed for repo %s", repo_id, exc_info=True)
        return []
