"""Read-only observatory for OpenAI Codex CLI rollout sessions.

Codex stores resumable JSONL rollouts below ``$CODEX_HOME/sessions`` (normally
``~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<session-id>.jsonl``).
This module presents those rollouts using the same normalized event vocabulary
as the Claude session observatory, while never returning developer instructions
or unredacted tool output to the browser.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from services.secret_rules import redact_secrets

logger = logging.getLogger(__name__)

__all__ = ["codex_home", "list_sessions", "tail_session"]

MAX_INITIAL_BYTES = 512 * 1024
MAX_APPEND_BYTES = 2 * 1024 * 1024
MAX_META_BYTES = 96 * 1024
MAX_TEXT_CHARS = 12_000
MAX_TOOL_RESULT_CHARS = 4_000
MAX_TOOL_SUMMARY_CHARS = 200
LIVE_WINDOW_SECONDS = 120.0

_SAFE_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_META_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
#: session id → rollout path. The tail endpoint is polled every 1.5 s; without
#: this every poll would ``rglob`` the whole ``sessions/`` tree. Filled by the
#: list scan and by a resolve miss; an entry whose file vanished is re-resolved.
_PATH_CACHE: dict[str, Path] = {}
_PATH_CACHE_MAX = 4096
#: Characters redacted past a text cap before the final cut (see ``_clean``).
_REDACT_MARGIN = 1024


def codex_home() -> Path:
    """Root of the Codex state directory on this host."""
    return Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex"))


def _sessions_dir() -> Path:
    return codex_home() / "sessions"


def _within_home(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(codex_home().resolve())
    except OSError, ValueError:
        return False


def _clean(value: Any, cap: int) -> tuple[str, bool, list[str]]:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError, ValueError:
            text = str(value)
    # Redact, then cap: capping first can sever a key so its prefix no longer
    # matches and leaks. Only ``cap + margin`` chars are redacted (bounded regex
    # work); if that pre-cut dropped text, the trailing margin — where a secret
    # severed by the pre-cut would sit unredacted — is discarded.
    truncated = len(text) > cap
    if len(text) > cap + _REDACT_MARGIN:
        redacted, kinds = redact_secrets(text[: cap + _REDACT_MARGIN])
        redacted = redacted[: max(0, len(redacted) - _REDACT_MARGIN)]
    else:
        redacted, kinds = redact_secrets(text)
    return redacted[:cap], truncated, kinds


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text", "output_text", "summary_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _tool_input(payload: dict[str, Any]) -> Any:
    raw = payload.get("arguments", payload.get("input", {}))
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _tool_summary(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "query", "description", "prompt", "url"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:MAX_TOOL_SUMMARY_CHARS]
    if isinstance(tool_input, str):
        return tool_input.strip()[:MAX_TOOL_SUMMARY_CHARS]
    return ""


def _read_meta(path: Path, mtime: float, size: int) -> dict[str, Any]:
    key = str(path)
    hit = _META_CACHE.get(key)
    if hit is not None and hit[:2] == (mtime, size):
        return hit[2]

    meta: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            chunk = fh.read(MAX_META_BYTES)
    except OSError:
        return meta

    for line in chunk.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "session_meta":
            meta["session_id"] = payload.get("id") or payload.get("session_id")
            meta["cwd"] = payload.get("cwd") or ""
            meta["version"] = payload.get("cli_version")
            meta["started_at"] = payload.get("timestamp") or obj.get("timestamp")
            meta["entrypoint"] = payload.get("originator") or payload.get("source")
            git = payload.get("git")
            if isinstance(git, dict):
                meta["git_branch"] = git.get("branch") or git.get("branch_name")
        elif obj.get("type") == "turn_context" and not meta.get("model"):
            meta["model"] = payload.get("model")
        elif (
            obj.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
            and not meta.get("title")
        ):
            title = " ".join(_content_text(payload.get("content")).split())
            if title:
                meta["title"] = _clean(title, 120)[0]

    if len(_META_CACHE) >= 512:
        _META_CACHE.clear()
    _META_CACHE[key] = (mtime, size, meta)
    return meta


def _scan_transcripts(limit: int) -> list[tuple[Path, os.stat_result]]:
    found: list[tuple[Path, os.stat_result]] = []
    try:
        candidates = _sessions_dir().rglob("rollout-*.jsonl")
        for path in candidates:
            try:
                if path.is_file() and _within_home(path):
                    found.append((path, path.stat()))
            except OSError:
                continue
    except OSError:
        return []
    found.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
    if len(_PATH_CACHE) > _PATH_CACHE_MAX:
        _PATH_CACHE.clear()
    for path, _ in reversed(found):  # oldest first, so the freshest copy wins
        sid = path.stem[-36:]
        if _SAFE_SESSION_ID.match(sid):
            _PATH_CACHE[sid] = path
    return found[: max(1, limit)]


def list_sessions(limit: int = 60, live_only: bool = False) -> list[dict[str, Any]]:
    """List Codex rollouts, newest first, in the shared session-row shape."""
    now = time.time()
    rows: list[dict[str, Any]] = []
    for path, st in _scan_transcripts(limit):
        meta = _read_meta(path, st.st_mtime, st.st_size)
        # The UUID is always the final 36 filename characters; metadata wins.
        session_id = str(meta.get("session_id") or path.stem[-36:])
        if not _SAFE_SESSION_ID.match(session_id):
            candidate = path.stem[-36:]
            if not _SAFE_SESSION_ID.match(candidate):
                continue
            session_id = candidate
        idle_for = max(0.0, now - st.st_mtime)
        live = idle_for <= LIVE_WINDOW_SECONDS
        if live_only and not live:
            continue
        cwd = str(meta.get("cwd") or "")
        rows.append(
            {
                "session_id": session_id,
                "cwd": cwd,
                "label": meta.get("title") or (os.path.basename(cwd) if cwd else session_id[:8]),
                "git_branch": meta.get("git_branch"),
                "version": meta.get("version"),
                "entrypoint": meta.get("entrypoint"),
                "kind": "codex",
                "pid": None,
                "status": "active" if live else "ended",
                "live": live,
                "started_at": meta.get("started_at"),
                "last_activity": st.st_mtime,
                "idle_seconds": round(idle_for, 1),
                "size_bytes": st.st_size,
                "task_id": None,
                "task_key": None,
                "task_status": None,
                "repo_id": None,
                "repo_name": None,
                "origin": "unknown",
                "model": meta.get("model"),
            }
        )
    return rows


def _resolve_transcript(session_id: str) -> Path | None:
    if not _SAFE_SESSION_ID.match(session_id or ""):
        return None
    cached = _PATH_CACHE.get(session_id)
    if cached is not None:
        try:
            if cached.is_file() and _within_home(cached):
                return cached
        except OSError:
            pass
        _PATH_CACHE.pop(session_id, None)
    matches: list[tuple[Path, float]] = []
    try:
        for path in _sessions_dir().rglob(f"*{session_id}.jsonl"):
            try:
                if path.is_file() and _within_home(path):
                    matches.append((path, path.stat().st_mtime))
            except OSError:
                continue
    except OSError:
        return None
    matches.sort(key=lambda pair: pair[1], reverse=True)
    if not matches:
        return None
    _PATH_CACHE[session_id] = matches[0][0]
    return matches[0][0]


def _parse_line(obj: dict[str, Any]) -> list[dict[str, Any]]:
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    timestamp = obj.get("timestamp") or payload.get("timestamp")
    events: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> None:
        event.setdefault("ts", timestamp)
        events.append(event)

    if obj.get("type") == "response_item":
        item_type = payload.get("type")
        if item_type == "message" and payload.get("role") in {"user", "assistant"}:
            raw = _content_text(payload.get("content"))
            if raw.strip():
                text, truncated, kinds = _clean(raw, MAX_TEXT_CHARS)
                emit(
                    {
                        "type": "text",
                        "role": payload["role"],
                        "text": text,
                        "truncated": truncated,
                        "redacted": kinds or None,
                    }
                )
        elif item_type == "reasoning":
            raw = _content_text(payload.get("summary"))
            if raw.strip():
                text, truncated, _ = _clean(raw, MAX_TEXT_CHARS)
                emit({"type": "thinking", "text": text, "truncated": truncated})
        elif item_type in {"function_call", "custom_tool_call"}:
            tool_input = _tool_input(payload)
            summary, _, kinds = _clean(_tool_summary(tool_input), MAX_TOOL_SUMMARY_CHARS)
            emit(
                {
                    "type": "tool_call",
                    "id": payload.get("call_id") or payload.get("id"),
                    "name": payload.get("name") or "tool",
                    "summary": summary,
                    "redacted": kinds or None,
                }
            )
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            raw = payload.get("output", "")
            text, truncated, kinds = _clean(raw, MAX_TOOL_RESULT_CHARS)
            emit(
                {
                    "type": "tool_result",
                    "tool_use_id": payload.get("call_id"),
                    "text": text,
                    "truncated": truncated,
                    "lines": str(raw).count("\n") + 1 if raw else 0,
                    "is_error": False,
                    "redacted": kinds or None,
                }
            )
    elif obj.get("type") == "event_msg":
        event_type = payload.get("type")
        if event_type == "token_count":
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or {}
            if usage:
                emit(
                    {
                        "type": "usage",
                        "msg_id": str(timestamp or obj.get("id") or ""),
                        "input_tokens": usage.get("input_tokens") or 0,
                        "output_tokens": usage.get("output_tokens") or 0,
                        "cache_read_tokens": usage.get("cached_input_tokens") or 0,
                        "cache_write_tokens": usage.get("cache_write_input_tokens") or 0,
                    }
                )
        elif event_type == "web_search_end":
            query = payload.get("query") or ""
            text, _, kinds = _clean(f"web search: {query}", MAX_TOOL_SUMMARY_CHARS)
            emit({"type": "notice", "text": text, "redacted": kinds or None})
    return events


def tail_session(session_id: str, since: int = 0, max_events: int = 3000) -> dict[str, Any]:
    """Tail one Codex JSONL rollout using the shared byte-offset protocol."""
    path = _resolve_transcript(session_id)
    if path is None:
        return {"events": [], "next_offset": 0, "size": 0, "exists": False}
    try:
        size = path.stat().st_size
    except OSError:
        return {"events": [], "next_offset": 0, "size": 0, "exists": False}

    start = max(0, int(since))
    reset = start > size
    if reset:
        start = 0
    skipped_head = False
    if start == 0 and size > MAX_INITIAL_BYTES:
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
        logger.warning("codex_sessions: read failed for %s: %s", session_id, exc)
        return {"events": [], "next_offset": since, "size": size, "exists": True}

    consumed = start
    if skipped_head:
        newline = raw.find(b"\n")
        if newline < 0:
            return {"events": [], "next_offset": consumed, "size": size, "exists": True}
        raw = raw[newline + 1 :]
        consumed += newline + 1
    last_newline = raw.rfind(b"\n")
    if last_newline < 0:
        return {"events": [], "next_offset": consumed, "size": size, "exists": True}
    consumed_end = consumed + last_newline + 1
    text = raw[:last_newline].decode("utf-8", errors="replace")

    events: list[dict[str, Any]] = []
    dropped = 0
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        try:
            parsed = _parse_line(obj)
        except Exception as exc:
            logger.debug("codex_sessions: unparsable line: %s", exc)
            continue
        if len(events) + len(parsed) > max_events:
            dropped += len(parsed)
        else:
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
