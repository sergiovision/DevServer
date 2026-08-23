"""Ask Agent — the dashboard's Copilot-style assistant panel.

One streaming endpoint backs the floating chat drawer on every dashboard page.
It has two engines behind a single normalised event stream:

- **chat** (free) — the configured System LLM over HTTP, token by token, via
  :mod:`services.llm_stream`. No tools, no side effects.
- **agent** (Pro) — the vendor's coding-agent CLI with the DevServer MCP server
  attached, so the assistant can search the operator's code, recall repo memory
  and read live task state — and *propose* side-effecting fixes the operator
  approves with one click.

Both emit the same event dicts (see :mod:`services.llm_stream`), so the browser
has exactly one renderer.

Deliberate design points:

- **This module is free-tier** and mounted unconditionally; the Pro engine is a
  soft import gated on ``licensing.is_pro_active()`` per request, so an
  unlicensed Pro build degrades to chat mode instead of silently keeping tools.
- **No database is required to answer.** The flagship Pro question is "Postgres
  is down, fix it" — an assistant that needs Postgres to answer that is useless,
  so settings reads fall back to ``.env`` defaults.
- Sessions live in memory with a TTL. The browser keeps the transcript; the
  worker keeps only what the CLI needs to resume.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services import agent_backends, assistant_kb, llm_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal")

# ── Pro engine (absent in the free build, gated by licence when present) ─────
try:  # pragma: no cover - import shape mirrors main.py / agent_runner.py
    from services.pro import assistant_agent as _agent_engine
    from services.pro import assistant_actions as _actions
    from services.pro import licensing as _licensing

    _HAS_PRO = True
except ImportError:  # free build
    _agent_engine = None  # type: ignore[assignment]
    _actions = None  # type: ignore[assignment]
    _licensing = None  # type: ignore[assignment]
    _HAS_PRO = False


# Vendors whose CLI emits structured streaming output we can translate. The
# Antigravity CLI (`agy`) has no --output-format at all, and codex's JSONL is
# only partially structured, so those fall back to chat mode.
_AGENT_VENDORS = {"anthropic", "glm"}

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
    mode: str = "auto"  # auto | chat | agent
    history: list[ChatMessageIn] = Field(default_factory=list)
    page: PageContextIn | None = None
    #: Per-turn model override, picked in the panel's toolbar. Must be one of
    #: the configured vendor's models; anything else falls back to the
    #: Settings value. The *vendor* is never overridable from the panel.
    model: str | None = None


class ActionDecisionRequest(BaseModel):
    decision: str  # approve | reject
    comment: str = ""
    follow_up: bool = True


class ProposeActionRequest(BaseModel):
    kind: str
    summary: str = ""
    params: dict = Field(default_factory=dict)
    session_id: str | None = None


# ── Session registry (in-memory by design — see module docstring) ────────────

class _Session:
    __slots__ = (
        "id", "created_at", "touched_at", "repo_id",
        "cli_session_id", "workdir", "mcp_config_path",
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

async def _read_system_llm() -> tuple[str, str, str]:
    """Return ``(vendor, model, mode)`` for the system LLM.

    Falls back to the built-in defaults when the settings table is unreachable —
    an assistant that cannot answer *because the database is down* is precisely
    useless in the one situation it is most needed.
    """
    defaults = ("glm", "glm-5.1", "max")
    try:
        from routes.internal import _read_system_llm_settings

        return await _read_system_llm_settings()
    except Exception as exc:  # noqa: BLE001 - degradation is the point
        logger.warning("Assistant: settings unreadable (%s); using defaults", exc)
        return defaults


def _pro_active() -> bool:
    if not _HAS_PRO or _licensing is None:
        return False
    try:
        return bool(_licensing.is_pro_active())
    except Exception:  # noqa: BLE001
        return False


def _vendor_models(vendor: str) -> list[dict[str, str]]:
    """The models the panel may offer for *vendor*.

    ``agent_backends.VENDOR_MODELS`` is the authority — the same registry the
    task form uses — so the assistant can never be pointed at a model the
    worker does not know how to run.
    """
    return agent_backends.VENDOR_MODELS.get(vendor, [])


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
    if any(m["id"] == requested for m in _vendor_models(vendor)):
        return requested, None
    return configured, (
        f"“{requested}” is not available for vendor {vendor} — "
        f"answering with {configured}."
    )


def _resolve_engine(requested: str, vendor: str) -> tuple[str, str | None]:
    """Decide which engine answers this turn. Returns ``(engine, notice)``."""
    pro = _pro_active()
    if requested == "chat":
        return "chat", None
    if requested == "agent" and not pro:
        return "chat", (
            "Tool access is a DevServer Pro feature — answering from the "
            "handbook instead."
        )
    if requested == "agent" and vendor not in _AGENT_VENDORS:
        return "chat", (
            f"The {vendor} CLI has no structured streaming output, so tools are "
            "unavailable on this vendor — answering from the handbook instead."
        )
    if requested == "auto":
        if pro and vendor in _AGENT_VENDORS:
            return "agent", None
        return "chat", None
    return "agent", None


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
        except Exception as exc:  # noqa: BLE001 - surface, never 500 mid-stream
            logger.exception("Assistant stream failed")
            with contextlib.suppress(Exception):
                await queue.put(
                    {"type": "error", "message": str(exc), "retryable": False}
                )
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
                    {"type": "error", "message": "stream time limit reached",
                     "retryable": True},
                    seq,
                )
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
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
    vendor, model, billing = await _read_system_llm()
    model, model_notice = _resolve_model(vendor, body.model, model)
    engine, notice = _resolve_engine((body.mode or "auto").lower(), vendor)

    page = body.page.model_dump() if body.page else None
    route = (page or {}).get("route") or (page or {}).get("path")
    topics = assistant_kb.select_topics(body.message, route)
    system_prompt = assistant_kb.build_system_prompt(
        topics,
        page,
        route=route,
        edition="pro" if _pro_active() else "free",
        tools_enabled=engine == "agent",
    )

    async def events() -> AsyncIterator[dict]:
        yield {
            "type": "session",
            "session_id": session.id,
            "engine": engine,
            "vendor": vendor,
            "model": model,
        }
        if model_notice:
            yield {"type": "notice", "text": model_notice}
        if notice:
            yield {"type": "notice", "text": notice}

        if engine == "agent" and _agent_engine is not None:
            async for event in _agent_engine.stream_agent_turn(
                session=session,
                user_text=body.message,
                system_prompt=system_prompt,
                vendor=vendor,
                model=model,
                billing_mode=billing,
            ):
                yield event
            return

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
    """What the panel can offer here — read by the UI to render its badge."""
    vendor, model, _mode = await _read_system_llm()
    pro = _pro_active()
    return {
        "edition": "pro" if pro else "free",
        "vendor": vendor,
        "model": model,
        # Models the panel's picker may offer — this vendor's only. `model`
        # above is the Settings default and the picker's initial value.
        "models": _vendor_models(vendor),
        "tools": pro and vendor in _AGENT_VENDORS,
        "agent_vendors": sorted(_AGENT_VENDORS),
        "actions": bool(pro and _actions is not None),
    }


@router.post("/assistant/sessions/{session_id}/cancel")
async def assistant_cancel(session_id: str):
    """Forget a session so the next turn starts a fresh agent conversation."""
    existed = _SESSIONS.pop(session_id, None) is not None
    return {"ok": True, "existed": existed}


@router.get("/assistant/actions")
async def assistant_actions(session_id: str | None = None):
    """Pending side-effect proposals awaiting the operator's decision (Pro)."""
    if not (_pro_active() and _actions is not None):
        return {"actions": []}
    return {"actions": [a.to_dict() for a in _actions.list_pending(session_id)]}


@router.post("/assistant/actions")
async def assistant_propose(body: ProposeActionRequest):
    """Record a side-effecting proposal for human approval (Pro).

    Called by the ``propose_action`` MCP tool inside the assistant's CLI
    subprocess. It performs **nothing** — it stores the proposal and returns an
    id. The operator's click is what runs anything, and the worker runs it, not
    the agent.
    """
    if not (_pro_active() and _actions is not None):
        raise HTTPException(404, "assistant actions require DevServer Pro")
    try:
        action = _actions.create(
            session_id=body.session_id or "",
            kind=body.kind,
            summary=body.summary,
            params=body.params,
        )
    except ValueError as exc:
        # Returned to the model as a tool error so it can correct itself rather
        # than silently believing it proposed something.
        raise HTTPException(400, str(exc))
    return {
        "status": "awaiting_approval",
        "action_id": action.id,
        "kind": action.kind,
        "summary": action.summary,
        "params": action.params,
        "risk": action.risk,
        "note": "Nothing has run. Stop here and end your turn.",
    }


@router.post("/assistant/actions/{action_id}/decide")
async def assistant_decide(
    action_id: str, body: ActionDecisionRequest, request: Request
):
    """Approve or reject a proposed action, streaming what happens next (Pro).

    Guarded by ``X-Internal-Token`` when ``INTERNAL_API_TOKEN`` is configured —
    this is the one assistant endpoint that executes something, and the worker
    has no authentication of its own.
    """
    if not (_pro_active() and _actions is not None):
        raise HTTPException(404, "assistant actions require DevServer Pro")
    _require_internal_token(request)

    try:
        action = _actions.decide(
            action_id, decision=body.decision, comment=body.comment
        )
    except KeyError:
        raise HTTPException(
            404, "this proposal is no longer pending — ask the assistant again"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    session = _SESSIONS.get(action.session_id)

    async def events() -> AsyncIterator[dict]:
        async for event in _actions.execute_and_resume(
            action,
            session=session,
            follow_up=body.follow_up,
            agent_engine=_agent_engine,
        ):
            yield event

    return StreamingResponse(
        _with_heartbeat(events(), request),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _require_internal_token(request: Request) -> None:
    """Enforce ``INTERNAL_API_TOKEN`` when one is configured.

    The worker binds 0.0.0.0 by default and has no auth on any route, so an
    endpoint that runs a maintenance script must not be reachable by anyone who
    can talk to the port. When the token is unset we log once rather than
    refusing — that would break every existing deployment on upgrade — but the
    deployment is then relying entirely on the port not being exposed.
    """
    from config import settings

    expected = (getattr(settings, "internal_api_token", "") or "").strip()
    if not expected:
        logger.warning(
            "Assistant action executed without INTERNAL_API_TOKEN set — set it "
            "in .env, and never expose the worker port."
        )
        return
    supplied = (request.headers.get("x-internal-token") or "").strip()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid or missing X-Internal-Token")
