"""Claude Code session observatory — watch every agent running on this host.

Claude Code persists two things under ``~/.claude`` that together make every
session on the machine observable without touching the sessions themselves:

  1. ``sessions/{pid}.json`` — a live registry of *interactive* CLI sessions::

         {"pid":3648,"sessionId":"f42f0824-…","cwd":"…/DevServer",
          "startedAt":1787319783267,"version":"2.1.238","kind":"interactive",
          "entrypoint":"cli","name":"devserver-2d","status":"busy",
          "updatedAt":1787319959924}

     Headless runs (``claude -p`` — every DevServer agent) do **not** register
     here, so this file is an optimisation for liveness/pid, never the index.

  2. ``projects/<slug(cwd)>/<sessionId>.jsonl`` — the append-only transcript of
     *every* session, headless included. This is the real index and the real
     content source.

The slug is lossy (both ``/`` and ``.`` collapse to ``-``), so it is never
reversed. Every ``user``/``assistant`` line carries an authoritative ``cwd``,
``gitBranch`` and ``version``, and that is what the metadata is read from.

Because the transcript is append-only, it tails exactly like ``worker.log``:
the byte-offset ``?since=`` protocol in ``apps/web/src/app/api/logs/[name]``
is reused verbatim, and only the per-line parser differs. Lines are
normalised into the same event vocabulary ``services/llm_stream.py`` emits
(``text`` / ``thinking`` / ``tool_call`` / ``tool_result`` / ``usage`` /
``notice``) so the browser has one renderer for Ask Agent and for this page.

Transcripts are raw agent output containing file contents and, on a
bad day, credentials — every text field goes through the shared
``secret_rules`` redactor before it leaves the worker.

Docker: the worker container cannot see the host's home directory. Bind-mount
it read-only and point ``CLAUDE_HOME`` at the mount::

    -v ${HOME}/.claude:/host-claude:ro  -e CLAUDE_HOME=/host-claude
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text as sa_text

from config import settings
from models.base import async_session
from services.secret_rules import redact_secrets

logger = logging.getLogger(__name__)

__all__ = [
    "active_task_for_session",
    "claude_home",
    "delete_session",
    "enrich_sessions",
    "list_sessions",
    "tail_session",
]


class SessionIsLiveError(RuntimeError):
    """Raised when a delete targets a session that is still running."""


class SessionDeleteFailedError(RuntimeError):
    """Raised when artefacts existed but not a single one could be removed."""


# ─── Limits ──────────────────────────────────────────────────────────────────

#: Transcript bytes returned on a first load (``since=0``) — the tail, so a
#: multi-megabyte session opens instantly at its most recent activity.
MAX_INITIAL_BYTES = 512 * 1024
#: Ceiling on a single incremental poll, so a burst of tool output cannot
#: return a 50 MB response.
MAX_APPEND_BYTES = 2 * 1024 * 1024
#: Head of a transcript scanned for metadata (cwd / version / title).
MAX_META_BYTES = 64 * 1024
#: Per-field text caps. Tool results are the long ones (file dumps, greps).
MAX_TEXT_CHARS = 12_000
MAX_TOOL_RESULT_CHARS = 4_000
MAX_TOOL_SUMMARY_CHARS = 200
#: A transcript whose file has not been touched for this long is not "live"
#: even if a stale registry entry still names it.
LIVE_WINDOW_SECONDS = 120.0

#: Task statuses in which the runner may still be driving a session — its
#: transcript must not be deleted out from under it.
_ACTIVE_TASK_STATUSES = ("queued", "running", "verifying")
#: Characters redacted past a text cap before the final cut (see ``_clean``).
_REDACT_MARGIN = 1024

#: Session ids are UUIDs. Anything else never reaches the filesystem.
_SAFE_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Transcript line types that carry no viewer-facing content. ``attachment``
#: is in here because it is context plumbing the CLI injects on every turn
#: (tool listings, token reminders) — noise that would bury the conversation.
_SKIP_TYPES = {
    "file-history-snapshot",
    "atis-latch",
    "last-prompt",
    "summary",
    "queued-command",
    "compact-boundary",
    "attachment",
}


# ─── Locations ───────────────────────────────────────────────────────────────


def claude_home() -> Path:
    """Root of the Claude Code state directory on this host."""
    return Path(os.getenv("CLAUDE_HOME") or (Path.home() / ".claude"))


def _projects_dir() -> Path:
    return claude_home() / "projects"


def _registry_dir() -> Path:
    return claude_home() / "sessions"


# ─── Live registry (interactive sessions only) ───────────────────────────────


def _pid_alive(pid: int) -> bool:
    """True when *pid* still exists. A crashed CLI leaves its registry file."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _read_registry() -> dict[str, dict[str, Any]]:
    """Map ``sessionId`` → registry record for every live interactive session."""
    out: dict[str, dict[str, Any]] = {}
    reg = _registry_dir()
    try:
        entries = list(reg.glob("*.json"))
    except OSError:
        return out

    for path in entries:
        try:
            rec = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except OSError, json.JSONDecodeError:
            continue
        sid = rec.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        pid = rec.get("pid")
        if isinstance(pid, int) and not _pid_alive(pid):
            continue  # stale file from a crashed CLI
        out[sid] = rec
    return out


# ─── Transcript metadata ─────────────────────────────────────────────────────

#: path → (mtime, size, meta). The head of an append-only file is immutable,
#: but ``ai-title`` can appear a few lines in, so the cache is invalidated on
#: growth rather than held forever.
_META_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_META_CACHE_MAX = 512


def _read_meta(path: Path, mtime: float, size: int) -> dict[str, Any]:
    """Parse the head of a transcript for its authoritative metadata.

    Returns ``cwd`` / ``git_branch`` / ``version`` / ``title`` / ``started_at``.
    Never raises — an unreadable or truncated transcript degrades to ``{}``.
    """
    key = str(path)
    hit = _META_CACHE.get(key)
    if hit is not None and hit[0] == mtime and hit[1] == size:
        return hit[2]

    meta: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            chunk = fh.read(MAX_META_BYTES)
    except OSError:
        return meta

    for line in chunk.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # last line of the chunk is usually a partial
        if not isinstance(obj, dict):
            continue

        if obj.get("type") == "ai-title" and obj.get("aiTitle"):
            meta["title"] = _clean(str(obj["aiTitle"]), 120)[0]
        for field, src in (("cwd", "cwd"), ("git_branch", "gitBranch"), ("version", "version")):
            if field not in meta and obj.get(src):
                meta[field] = str(obj[src])
        if "started_at" not in meta and obj.get("timestamp"):
            meta["started_at"] = str(obj["timestamp"])
        if "entrypoint" not in meta and obj.get("entrypoint"):
            meta["entrypoint"] = str(obj["entrypoint"])

    if len(_META_CACHE) >= _META_CACHE_MAX:
        _META_CACHE.clear()
    _META_CACHE[key] = (mtime, size, meta)
    return meta


# ─── Listing ─────────────────────────────────────────────────────────────────


def _scan_transcripts(limit: int) -> list[tuple[Path, os.stat_result]]:
    """Most-recently-active transcripts across every project, newest first."""
    root = _projects_dir()
    found: list[tuple[Path, os.stat_result]] = []
    try:
        project_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return found

    for pdir in project_dirs:
        try:
            for f in pdir.glob("*.jsonl"):
                try:
                    found.append((f, f.stat()))
                except OSError:
                    continue
        except OSError:
            continue

    found.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
    return found[:limit]


def list_sessions(limit: int = 60, live_only: bool = False) -> list[dict[str, Any]]:
    """Every Claude session on this host, most recently active first.

    Merges the transcript scan (the index — covers headless agents and history)
    with the live registry (pid, ``busy``/``idle``, operator-visible name).
    """
    registry = _read_registry()
    now = time.time()
    sessions: list[dict[str, Any]] = []

    for path, st in _scan_transcripts(max(limit, 1)):
        sid = path.stem
        if not _SAFE_SESSION_ID.match(sid):
            continue

        meta = _read_meta(path, st.st_mtime, st.st_size)
        reg = registry.get(sid)
        idle_for = now - st.st_mtime
        is_live = reg is not None or idle_for <= LIVE_WINDOW_SECONDS

        if live_only and not is_live:
            continue

        cwd = meta.get("cwd") or ""
        sessions.append(
            {
                "session_id": sid,
                "cwd": cwd,
                "label": (
                    meta.get("title")
                    or (reg or {}).get("name")
                    or (os.path.basename(cwd) if cwd else sid[:8])
                ),
                "git_branch": meta.get("git_branch"),
                "version": meta.get("version") or (reg or {}).get("version"),
                "entrypoint": meta.get("entrypoint") or (reg or {}).get("entrypoint"),
                "kind": (reg or {}).get("kind") or "headless",
                "pid": (reg or {}).get("pid"),
                # 'busy' only ever comes from the registry; a headless agent that
                # wrote in the last few seconds is reported as 'active'.
                "status": (
                    (reg or {}).get("status")
                    or ("active" if idle_for <= LIVE_WINDOW_SECONDS else "ended")
                ),
                "live": is_live,
                "started_at": meta.get("started_at"),
                "last_activity": st.st_mtime,
                "idle_seconds": round(idle_for, 1),
                "size_bytes": st.st_size,
                # Filled in by enrich_sessions when a DB session is available.
                "task_id": None,
                "task_key": None,
                "task_status": None,
                "repo_id": None,
                "repo_name": None,
                "origin": "unknown",
            }
        )

    return sessions


# ─── DB enrichment: which sessions are DevServer's own agents ────────────────


def _task_key_from_path(cwd: str) -> str | None:
    """Derive a task key from the directory an agent was launched in.

    Fallback for sessions whose id never made it into ``task_runs`` (a run that
    crashed before the id was parsed out of the CLI's JSON envelope). Covers
    the two layouts the runners use: ``{worktree_dir}/{TASK-KEY}`` for heavy
    tasks and ``{log_dir}/skill-work-{TASK-KEY}`` for light ones.
    """
    if not cwd:
        return None
    wt = (settings.worktree_dir or "").rstrip("/")
    if wt and cwd.startswith(wt + "/"):
        seg = cwd[len(wt) + 1 :].split("/", 1)[0]
        if seg and not seg.startswith("."):
            return seg
    base = os.path.basename(cwd.rstrip("/"))
    if base.startswith("skill-work-"):
        return base[len("skill-work-") :] or None
    return None


async def enrich_sessions(db, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach task/repo attribution to a session list.

    ``task_runs.session_id`` is the authoritative join — it is written by the
    agent runner for every DevServer-launched agent. Path heuristics only fill
    the gaps, and a plain ``cwd`` prefix match labels sessions belonging to a
    registered local repo. Never raises: attribution is a nicety, a failed
    lookup must not blank out the page.
    """
    if not sessions:
        return sessions

    by_id = {s["session_id"]: s for s in sessions}

    try:
        rows = (
            (
                await db.execute(
                    sa_text(
                        """
                SELECT DISTINCT ON (tr.session_id)
                       tr.session_id, t.id AS task_id, t.task_key, t.status, t.repo_id, r.name AS repo_name
                  FROM task_runs tr
                  JOIN tasks t ON t.id = tr.task_id
             LEFT JOIN repos r ON r.id = t.repo_id
                 WHERE tr.session_id = ANY(:ids)
              ORDER BY tr.session_id, tr.id DESC
                """
                    ),
                    {"ids": list(by_id.keys())},
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            s = by_id.get(row["session_id"])
            if s is None:
                continue
            s["task_id"] = row["task_id"]
            s["task_key"] = row["task_key"]
            s["task_status"] = row["status"]
            s["repo_id"] = row["repo_id"]
            s["repo_name"] = row["repo_name"]
            s["origin"] = "devserver"
    except Exception as exc:
        logger.debug("claude_sessions: task attribution failed: %s", exc)

    # Repo roots for cwd-prefix matching. ``gitea_url`` doubles as the local
    # root folder for provider='local' (see CLAUDE.md), hence the isabs test.
    repo_roots: list[tuple[str, int, str]] = []
    try:
        rows = (
            (await db.execute(sa_text("SELECT id, name, gitea_url FROM repos WHERE active = TRUE")))
            .mappings()
            .all()
        )
        for row in rows:
            root = (row["gitea_url"] or "").rstrip("/")
            if root and os.path.isabs(root):
                repo_roots.append((root, row["id"], row["name"]))
        repo_roots.sort(key=lambda r: len(r[0]), reverse=True)  # longest match wins
    except Exception as exc:
        logger.debug("claude_sessions: repo lookup failed: %s", exc)

    devserver_root = os.path.abspath(
        os.getenv("DEVSERVER_ROOT")
        # this file is <root>/apps/worker/src/services/ — four levels up
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
    )

    for s in sessions:
        cwd = s.get("cwd") or ""
        if not s.get("task_key"):
            derived = _task_key_from_path(cwd)
            if derived:
                s["task_key"] = derived
                s["origin"] = "devserver"
        if s.get("repo_id") is None:
            for root, rid, rname in repo_roots:
                if cwd == root or cwd.startswith(root + "/"):
                    s["repo_id"] = rid
                    s["repo_name"] = rname
                    break
        if s["origin"] == "unknown":
            if os.path.basename(cwd).startswith("assistant-"):
                s["origin"] = "assistant"  # Ask Agent scratch session
            elif (
                s.get("repo_id") is not None
                or cwd == devserver_root
                or cwd.startswith(devserver_root + "/")
            ):
                # An operator console sitting in a repo DevServer knows about.
                s["origin"] = "console"
            else:
                s["origin"] = "external"  # some other project on the host

    return sessions


# ─── Transcript resolution ───────────────────────────────────────────────────


def _within_home(path: Path) -> bool:
    """True when *path* really lives under CLAUDE_HOME.

    Resolved, not string-compared: a project directory (or a session-env entry)
    can be a symlink pointing anywhere, and this guard is what stands between a
    session id and an arbitrary file on the host.
    """
    try:
        return path.resolve().is_relative_to(claude_home().resolve())
    except OSError, ValueError:
        return False


def _transcript_paths(session_id: str) -> list[Path]:
    """Every transcript file for a session, freshest first.

    A session id appears under two project slugs when the agent changed
    directory mid-run, so this returns a list; readers take the first,
    deletion takes them all. ``session_id`` is validated against a strict
    UUID pattern before it touches the filesystem.
    """
    if not _SAFE_SESSION_ID.match(session_id or ""):
        return []

    candidates: list[tuple[Path, float]] = []
    try:
        for pdir in _projects_dir().iterdir():
            if not pdir.is_dir():
                continue
            f = pdir / f"{session_id}.jsonl"
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if _within_home(f):
                candidates.append((f, mtime))
    except OSError:
        return []

    candidates.sort(key=lambda pair: pair[1], reverse=True)
    return [p for p, _ in candidates]


def _resolve_transcript(session_id: str) -> Path | None:
    """The transcript to read for a session — the freshest copy, or None."""
    paths = _transcript_paths(session_id)
    return paths[0] if paths else None


# ─── Line → event parsing ────────────────────────────────────────────────────


def _clean(value: Any, cap: int) -> tuple[str, bool, list[str]]:
    """Redact, then cap. Returns ``(text, truncated, redaction_kinds)``.

    Redacting before the cut matters: capping first can slice a key so the
    remaining prefix no longer matches its pattern and leaks verbatim. To keep
    the regex work bounded on a 10 MB tool result, only ``cap + margin`` chars
    are redacted; when that pre-cut actually dropped text, the trailing margin
    (where a secret severed by the pre-cut would sit, untouched by redaction)
    is discarded before the final cap.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return _redact_capped(text, cap)


def _redact_capped(text: str, cap: int) -> tuple[str, bool, list[str]]:
    truncated = len(text) > cap
    if len(text) > cap + _REDACT_MARGIN:
        redacted, kinds = redact_secrets(text[: cap + _REDACT_MARGIN])
        redacted = redacted[: max(0, len(redacted) - _REDACT_MARGIN)]
    else:
        redacted, kinds = redact_secrets(text)
    return redacted[:cap], truncated, kinds


def _tool_summary(name: str, tool_input: Any) -> str:
    """One-line gist of a tool call — what the collapsed row shows."""
    if not isinstance(tool_input, dict):
        return ""
    for key in (
        "command",
        "file_path",
        "pattern",
        "path",
        "query",
        "description",
        "prompt",
        "url",
        "notebook_path",
    ):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:MAX_TOOL_SUMMARY_CHARS]
    for val in tool_input.values():
        if isinstance(val, str) and val.strip():
            return val.strip()[:MAX_TOOL_SUMMARY_CHARS]
    return ""


def _tool_result_text(content: Any) -> str:
    """Flatten a ``tool_result`` payload, which is a string or a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _parse_line(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise one transcript line into zero or more viewer events."""
    kind = obj.get("type")
    if kind in _SKIP_TYPES:
        return []

    ts = obj.get("timestamp")
    sidechain = bool(obj.get("isSidechain"))
    events: list[dict[str, Any]] = []

    def emit(ev: dict[str, Any]) -> None:
        ev.setdefault("ts", ts)
        if sidechain:
            ev["sidechain"] = True
        events.append(ev)

    if kind in ("mode", "permission-mode"):
        value = obj.get("mode") or obj.get("permissionMode")
        if value:
            label = "mode" if kind == "mode" else "permission mode"
            text, _, kinds = _clean(f"{label}: {value}", MAX_TOOL_SUMMARY_CHARS)
            emit({"type": "notice", "text": text, "redacted": kinds or None})
        return events

    if kind == "ai-title":
        if obj.get("aiTitle"):
            text, _, kinds = _clean(f"title: {obj['aiTitle']}", MAX_TOOL_SUMMARY_CHARS)
            emit({"type": "notice", "text": text, "redacted": kinds or None})
        return events

    if kind not in ("user", "assistant"):
        return events

    message = obj.get("message")
    if not isinstance(message, dict):
        return events

    content = message.get("content")
    model = message.get("model")

    if isinstance(content, str):
        if content.strip():
            text, truncated, kinds = _clean(content, MAX_TEXT_CHARS)
            emit(
                {
                    "type": "text",
                    "role": kind,
                    "text": text,
                    "truncated": truncated,
                    "redacted": kinds or None,
                }
            )
        return events

    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            raw = block.get("text") or ""
            if not raw.strip():
                continue
            text, truncated, kinds = _clean(raw, MAX_TEXT_CHARS)
            emit(
                {
                    "type": "text",
                    "role": kind,
                    "text": text,
                    "model": model,
                    "truncated": truncated,
                    "redacted": kinds or None,
                }
            )

        elif btype == "thinking":
            raw = block.get("thinking") or ""
            if not raw.strip():
                continue  # signature-only block — nothing to show
            text, truncated, _ = _clean(raw, MAX_TEXT_CHARS)
            emit({"type": "thinking", "text": text, "truncated": truncated})

        elif btype == "tool_use":
            tool_input = block.get("input")
            summary, _, kinds = _clean(
                _tool_summary(block.get("name") or "", tool_input), MAX_TOOL_SUMMARY_CHARS
            )
            emit(
                {
                    "type": "tool_call",
                    "id": block.get("id"),
                    "name": block.get("name") or "tool",
                    "summary": summary,
                    "redacted": kinds or None,
                }
            )

        elif btype == "tool_result":
            raw = _tool_result_text(block.get("content"))
            text, truncated, kinds = _clean(raw, MAX_TOOL_RESULT_CHARS)
            emit(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "text": text,
                    "truncated": truncated,
                    "lines": raw.count("\n") + 1 if raw else 0,
                    "is_error": bool(block.get("is_error")),
                    "redacted": kinds or None,
                }
            )

        elif btype == "image":
            emit({"type": "notice", "text": "[image]"})

    # One assistant message is written across several transcript lines (one per
    # streamed content block), each repeating the identical usage envelope.
    # Tag it with the message id so the batch — and the browser, across polls —
    # can count each turn exactly once.
    usage = message.get("usage")
    if isinstance(usage, dict):
        emit(
            {
                "type": "usage",
                "msg_id": message.get("id"),
                "model": model,
                "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
                "cache_write_tokens": usage.get("cache_creation_input_tokens") or 0,
            }
        )

    return events


# ─── Delete ──────────────────────────────────────────────────────────────────


async def active_task_for_session(session_id: str) -> str | None:
    """Task key of a queued/running/verifying task that owns *session_id*.

    Headless agents never register in ``sessions/``, and a long tool call can
    keep the transcript quiet for longer than ``LIVE_WINDOW_SECONDS`` — the
    ``task_runs`` join is what reliably says "DevServer is still driving this".
    Opens its own DB session. Raises on a DB failure: the caller must refuse
    the delete rather than guess.
    """
    if not _SAFE_SESSION_ID.match(session_id or ""):
        return None  # delete_session rejects it; no need to hit the DB
    async with async_session() as db:
        row = (
            await db.execute(
                sa_text(
                    """
                SELECT t.task_key
                  FROM task_runs tr
                  JOIN tasks t ON t.id = tr.task_id
                 WHERE tr.session_id = :sid
                   AND t.status = ANY(:statuses)
                 LIMIT 1
                """
                ),
                {"sid": session_id, "statuses": list(_ACTIVE_TASK_STATUSES)},
            )
        ).first()
    return row[0] if row else None


def delete_session(session_id: str) -> dict[str, Any]:
    """Erase a session from disk. **Irreversible** — there is no trash.

    Removes all three artefacts Claude Code names after a session id: the
    transcript(s) under ``projects/``, the ``session-env/<id>/`` directory and
    the ``file-history/<id>/`` snapshot directory. The live registry
    (``sessions/<pid>.json``) is deliberately left alone — those files belong
    to running processes, not to us.

    Refuses a live session: its CLI holds the transcript open and would lose
    both its history and its ability to ``--resume``. Stop the session first.

    Raises ``ValueError`` on a malformed id, ``FileNotFoundError`` when nothing
    matches, ``SessionIsLiveError`` when the session is still running and
    ``SessionDeleteFailedError`` when artefacts exist but none could be removed.
    Callers should also check ``active_task_for_session`` first.
    """
    if not _SAFE_SESSION_ID.match(session_id or ""):
        raise ValueError("malformed session id")

    transcripts = _transcript_paths(session_id)

    # Liveness: the registry is authoritative for interactive sessions, and a
    # very recent write catches headless agents that never register.
    if session_id in _read_registry():
        raise SessionIsLiveError("session is running (registered with a live pid)")
    for path in transcripts:
        try:
            if time.time() - path.stat().st_mtime <= LIVE_WINDOW_SECONDS:
                raise SessionIsLiveError("session wrote to its transcript moments ago")
        except OSError:
            continue

    # Directories named after the session id. Each is containment-checked
    # again immediately before removal, since it is resolved fresh here.
    extra_dirs = [
        claude_home() / "session-env" / session_id,
        claude_home() / "file-history" / session_id,
    ]

    removed: list[str] = []
    errors: list[str] = []
    freed = 0

    for path in transcripts:
        with contextlib.suppress(OSError):
            freed += path.stat().st_size
        try:
            path.unlink()
            removed.append(str(path))
            _META_CACHE.pop(str(path), None)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")

    for d in extra_dirs:
        if not d.is_dir() or not _within_home(d):
            continue
        try:
            shutil.rmtree(d)
            removed.append(str(d))
        except OSError as exc:
            errors.append(f"{d.name}: {exc}")

    if not removed and not errors:
        raise FileNotFoundError(f"no on-disk artefacts for session {session_id}")
    if not removed:
        raise SessionDeleteFailedError("nothing could be removed: " + "; ".join(errors))

    logger.info(
        "claude_sessions: deleted %s (%d artefacts, %d bytes)", session_id, len(removed), freed
    )
    return {
        "session_id": session_id,
        "deleted": len(removed),
        "paths": removed,
        "freed_bytes": freed,
        "errors": errors,
    }


# ─── Tail ────────────────────────────────────────────────────────────────────


def tail_session(session_id: str, since: int = 0, max_events: int = 3000) -> dict[str, Any]:
    """Read a transcript from byte offset *since* and return parsed events.

    The offset protocol matches the Logs page exactly: ``nextOffset`` is the
    byte position of the last **complete** line consumed, so a partially
    written line is re-read on the next poll rather than parsed as garbage. A
    file that shrank (rotated, or a session restarted) resets the caller to 0.
    """
    path = _resolve_transcript(session_id)
    if path is None:
        return {"events": [], "next_offset": 0, "size": 0, "exists": False}

    try:
        size = path.stat().st_size
    except OSError:
        return {"events": [], "next_offset": 0, "size": 0, "exists": False}

    start = max(0, int(since))
    reset = False
    if start > size:  # truncated/rotated — start over
        start, reset = 0, True

    first_load = start == 0
    skipped_head = False
    if first_load and size > MAX_INITIAL_BYTES:
        start = size - MAX_INITIAL_BYTES
        skipped_head = True
    if size - start > MAX_APPEND_BYTES:
        start = size - MAX_APPEND_BYTES
        skipped_head = True

    if size <= start:
        return {"events": [], "next_offset": size, "size": size, "exists": True, "reset": reset}

    try:
        with path.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(size - start)
    except OSError as exc:
        logger.warning("claude_sessions: read failed for %s: %s", session_id, exc)
        return {"events": [], "next_offset": since, "size": size, "exists": True}

    # Line splitting happens on bytes, never on the decoded string: transcripts
    # are full of multibyte UTF-8 and a character-based offset would drift out
    # of sync with the file, re-reading or skipping content on every poll.
    consumed = start

    # Drop a leading partial line — but ONLY when we seeked to an arbitrary
    # byte. Resuming from a `since` we handed out lands exactly after a
    # newline, and stripping there would silently swallow the first complete
    # line on every single poll.
    if skipped_head:
        nl = raw.find(b"\n")
        if nl < 0:
            return {"events": [], "next_offset": consumed, "size": size, "exists": True}
        raw = raw[nl + 1 :]
        consumed += nl + 1

    # Defer a trailing partial line to the next poll.
    last_nl = raw.rfind(b"\n")
    if last_nl < 0:
        return {"events": [], "next_offset": consumed, "size": size, "exists": True}
    consumed_end = consumed + last_nl + 1
    text = raw[:last_nl].decode("utf-8", errors="replace")

    events: list[dict[str, Any]] = []
    seen_usage: set[str] = set()
    dropped = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        try:
            parsed = _parse_line(obj)
        except Exception as exc:
            logger.debug("claude_sessions: unparsable line: %s", exc)
            continue
        kept: list[dict[str, Any]] = []
        for ev in parsed:
            if ev["type"] == "usage":
                msg_id = ev.get("msg_id")
                if msg_id:
                    if msg_id in seen_usage:
                        continue
                    seen_usage.add(msg_id)
            kept.append(ev)
        parsed = kept

        if not parsed:
            continue
        if len(events) + len(parsed) > max_events:
            dropped += len(parsed)
            continue
        events.extend(parsed)

    return {
        "events": events,
        "next_offset": consumed_end,
        "size": size,
        "exists": True,
        "reset": reset,
        "truncated_head": skipped_head,
        "dropped": dropped,
    }
