"""Ask Agent — the dashboard's assistant panel.

One streaming endpoint backs the chat drawer on every dashboard page: the
configured System LLM answers over HTTP, token by token, via
:mod:`services.llm_stream`, grounded in the built-in handbook
(:mod:`services.assistant_kb`). It has no tools and no side effects.

Deliberate design points:

- **No database is required to answer.** "Postgres is down, what now?" is
  exactly when the panel is needed, so settings reads fall back to the
  registry defaults.
- Sessions live in memory with a TTL. The browser keeps the transcript; the
  worker keeps only what it needs to continue a conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from models.base import async_session
from services import agent_backends, assistant_kb, llm_stream, system_llm
from services.system_llm import SystemLLM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal")

_HEARTBEAT_SECONDS = 15.0
_MAX_STREAM_SECONDS = 20 * 60
_SESSION_TTL_SECONDS = 2 * 60 * 60
_MAX_HISTORY_TURNS = 20

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx buffers text/event-stream by default, which turns a live stream into
    # one lump at the end. Same header set as lib/a2a/stream.ts.
    "X-Accel-Buffering": "no",
}


# ── Request models ───────────────────────────────────────────────────────────


class ChatMessageIn(BaseModel):
    role: str = "user"
    content: str = ""


class PageContextIn(BaseModel):
    path: str | None = None
    route: str | None = None
    title: str | None = None
    entity: dict | None = None


class AssistantChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    repo_id: int | None = None
    #: Accepted for API compatibility; this edition always answers in chat mode.
    mode: str = "chat"
    history: list[ChatMessageIn] = Field(default_factory=list)
    page: PageContextIn | None = None
    #: Per-turn model override, picked in the panel's toolbar. Must be one of
    #: the configured vendor's models; anything else falls back to the
    #: Settings value. The *vendor* is never overridable from the panel.
    model: str | None = None


# ── Session registry (in-memory by design — see module docstring) ────────────


class _Session:
    __slots__ = (
        "cli_session_id",
        "created_at",
        "id",
        "mcp_config_path",
        "repo_id",
        "touched_at",
        "workdir",
    )

    def __init__(self, session_id: str, repo_id: int | None) -> None:
        self.id = session_id
        self.repo_id = repo_id
        self.created_at = time.time()
        self.touched_at = self.created_at
        # Set by the Pro agent engine on the first turn and reused thereafter,
        # so `--resume` finds the same conversation and MCP config.
        self.cli_session_id: str | None = None
        self.workdir: str | None = None
        self.mcp_config_path: str | None = None


_SESSIONS: dict[str, _Session] = {}


def _sweep_sessions() -> None:
    cutoff = time.time() - _SESSION_TTL_SECONDS
    for key in [k for k, s in _SESSIONS.items() if s.touched_at < cutoff]:
        _SESSIONS.pop(key, None)


def _get_session(session_id: str | None, repo_id: int | None) -> _Session:
    _sweep_sessions()
    if session_id and session_id in _SESSIONS:
        sess = _SESSIONS[session_id]
        sess.touched_at = time.time()
        if repo_id is not None:
            sess.repo_id = repo_id
        return sess
    sess = _Session(session_id or secrets.token_urlsafe(12), repo_id)
    _SESSIONS[sess.id] = sess
    return sess


# ── Configuration reads that survive a dead database ─────────────────────────


async def _read_system_llm() -> SystemLLM:
    """The configured System LLM, or the registry defaults without a database.

    An assistant that cannot answer *because the database is down* is useless
    in exactly the situation it is most needed.
    """
    try:
        async with async_session() as db:
            return await system_llm.read(db)
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Assistant: settings unreadable (%s); using defaults", exc)
        return SystemLLM()


def _vendor_models(vendor: str) -> list[dict[str, str]]:
    """The models the panel may offer for *vendor*.

    The model registry is the authority — the same one the task form uses — so
    the assistant can never be pointed at a model the worker cannot run.
    """
    return [{"id": m.id, "label": m.label} for m in agent_backends.picker_models(vendor)]


def _resolve_model(vendor: str, requested: str | None, configured: str) -> tuple[str, str | None]:
    """Pick this turn's model, returning ``(model, notice)``.

    Only the model is selectable in the panel; the vendor stays whatever
    Settings → System LLM says, because switching vendor also switches
    credentials, billing mode and engine eligibility. An unknown id falls back
    to the configured model rather than erroring — a stale value in the
    browser (registry changed under it) must not break the chat, but the
    operator should be told which model actually answered.
    """
    requested = (requested or "").strip()
    if not requested or requested == configured:
        return configured, None
    if any(m.id == requested for m in agent_backends.VENDOR_MODELS.get(vendor, ())):
        return requested, None
    return configured, (
        f"“{requested}” is not available for vendor {vendor} — answering with {configured}."
    )


# ── SSE framing ──────────────────────────────────────────────────────────────


def _sse(obj: dict, seq: int) -> str:
    """One SSE frame. Unnamed ``data:`` only — the kind lives in ``type``.

    ``json.dumps`` escapes newlines, so a multi-line delta can never break
    framing. The ``id:`` is a monotonic sequence so ``Last-Event-ID`` resume can
    be added later without a format change.
    """
    return f"id: {seq}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def _with_heartbeat(
    source: AsyncIterator[dict],
    request: Request,
    *,
    interval: float = _HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Frame ``source`` as SSE, injecting keep-alives during quiet stretches.

    Pumping into a queue rather than wrapping each ``__anext__`` in
    ``asyncio.wait_for`` matters: the naive version *discards the in-flight
    item* when the timeout fires. The bounded queue doubles as backpressure —
    a stalled browser eventually blocks the agent subprocess instead of letting
    the worker balloon.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    async def pump() -> None:
        try:
            async for event in source:
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Assistant stream failed")
            with contextlib.suppress(Exception):
                await queue.put({"type": "error", "message": str(exc), "retryable": False})
        finally:
            with contextlib.suppress(Exception):
                await queue.put(None)

    task = asyncio.create_task(pump())
    seq = 0
    deadline = time.monotonic() + _MAX_STREAM_SECONDS
    try:
        while True:
            if time.monotonic() > deadline:
                seq += 1
                yield _sse(
                    {"type": "error", "message": "stream time limit reached", "retryable": True},
                    seq,
                )
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                if await request.is_disconnected():
                    break
                yield ": ping\n\n"  # SSE comment; every client ignores it
                continue
            if event is None:
                break
            seq += 1
            yield _sse(event, seq)
    finally:
        # Reached on completion, on client disconnect (GeneratorExit) and on
        # cancellation. Cancelling the pump propagates into the engine's own
        # finally block, which is what kills an agent subprocess when the
        # operator closes the tab.
        task.cancel()
        with contextlib.suppress(Exception):
            await task


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/assistant/chat/stream")
async def assistant_chat_stream(body: AssistantChatRequest, request: Request):
    """Answer one assistant turn as a stream of normalised events."""
    session = _get_session(body.session_id, body.repo_id)
    llm = await _read_system_llm()
    vendor = llm.vendor
    model, model_notice = _resolve_model(vendor, body.model, llm.model)

    page = body.page.model_dump() if body.page else None
    route = (page or {}).get("route") or (page or {}).get("path")
    topics = assistant_kb.select_topics(body.message, route)
    system_prompt = assistant_kb.build_system_prompt(
        topics,
        page,
        route=route,
    )

    async def events() -> AsyncIterator[dict]:
        yield {
            "type": "session",
            "session_id": session.id,
            "engine": "chat",
            "vendor": vendor,
            "model": model,
        }
        if model_notice:
            yield {"type": "notice", "text": model_notice}

        history = [
            {"role": m.role, "content": m.content}
            for m in body.history[-_MAX_HISTORY_TURNS:]
            if (m.content or "").strip()
        ]
        history.append({"role": "user", "content": body.message})
        async for event in llm_stream.stream_chat(
            vendor=vendor,
            model=model,
            messages=history,
            system=system_prompt,
            max_tokens=4096,
        ):
            yield event

    return StreamingResponse(
        _with_heartbeat(events(), request),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/assistant/capabilities")
async def assistant_capabilities():
    """What the panel can offer here — read by the UI to render its picker."""
    llm = await _read_system_llm()
    return {
        "vendor": llm.vendor,
        "model": llm.model,
        # The picker offers this vendor's models; `model` is its initial value.
        "models": _vendor_models(llm.vendor),
        "tools": False,
    }


@router.post("/assistant/sessions/{session_id}/cancel")
async def assistant_cancel(session_id: str):
    """Forget a session so the next turn starts a fresh agent conversation."""
    existed = _SESSIONS.pop(session_id, None) is not None
    return {"ok": True, "existed": existed}
