"""Streaming sibling of :mod:`services.llm_client` — token deltas over SSE.

``llm_client.complete`` / ``complete_chat`` buffer a whole answer before
returning, which is fine for Fill Task or a memory rerank but reads as a dead
UI for a chat panel. This module streams the same four vendors incrementally
and normalises three quite different wire formats into **one event vocabulary**
that the Ask Agent panel understands:

    {"type": "start",          "vendor", "model", "transport"}
    {"type": "text_delta",     "text"}
    {"type": "thinking_delta", "text"}
    {"type": "tool_call",      "id", "name", "input"}
    {"type": "tool_result",    "id", "name", "ok", "preview"}
    {"type": "notice",         "text"}
    {"type": "usage",          "input_tokens", "output_tokens"}
    {"type": "error",          "message", "retryable"}
    {"type": "done",           "stop_reason", "text", "session_id", "awaiting"}

The agentic (CLI + MCP) path in ``services.pro.assistant_agent`` emits the
*same* dicts, so the browser has exactly one ``JSON.parse`` branch and one
renderer regardless of which engine answered.

Generators here never raise for a vendor-side failure — they yield an ``error``
event and stop. A dead stream in a chat panel must still say why.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from services.llm_client import _BUILDERS, ChatMessage, resolve_api_key

logger = logging.getLogger(__name__)

__all__ = ["stream_chat", "NormEvent", "AnthropicDeltaTracker"]

NormEvent = dict

# Status codes worth retrying: rate limits and transient upstream failures.
# 529 is Anthropic's "overloaded".
_RETRYABLE = {408, 429, 500, 502, 503, 504, 529}


# ── SSE frame reassembly ─────────────────────────────────────────────────────

async def _iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    """Yield ``(event_name, data)`` per blank-line-delimited SSE frame.

    Handles the two shapes in play: Anthropic sends named frames
    (``event: content_block_delta``), OpenAI and Google send unnamed ones. A
    frame may carry several ``data:`` lines, which per the SSE spec are joined
    with newlines.
    """
    event_name: str | None = None
    data_lines: list[str] = []
    async for raw in resp.aiter_lines():
        line = raw.rstrip("\r")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):  # comment / keep-alive
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _loads(payload: str) -> dict | None:
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


# ── Per-vendor translators ───────────────────────────────────────────────────
#
# Each is a generator over SSE frames that yields normalised events and keeps a
# running copy of the full text, so the terminal ``done`` always carries the
# complete answer even when the consumer dropped deltas.

class AnthropicDeltaTracker:
    """Turns raw Anthropic streaming events into normalised events.

    Split out of the HTTP translator because the Claude Code CLI's
    ``stream-json`` output wraps these *exact same* events in
    ``{"type":"stream_event","event":{…}}`` lines. The agentic path in
    ``services.pro.assistant_agent`` feeds them through here rather than owning
    a second, divergent copy of the tool-JSON reassembly logic.

    Stateful across a whole response: partial ``input_json_delta`` fragments are
    buffered per block index and emitted as one complete ``tool_call`` when the
    block closes.
    """

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.blocks: dict[int, dict] = {}
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.stop_reason = "end_turn"
        self.error: NormEvent | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def handle(self, data: dict, name: str | None = None) -> list[NormEvent]:
        """Process one event; return the normalised events it produces."""
        out: list[NormEvent] = []
        kind = name or data.get("type") or ""

        if kind == "message_start":
            u = (data.get("message") or {}).get("usage") or {}
            if u.get("input_tokens"):
                self.usage["input_tokens"] += u["input_tokens"] or 0
        elif kind == "content_block_start":
            block = data.get("content_block") or {}
            self.blocks[data.get("index", 0)] = {
                "type": block.get("type", "text"),
                "name": block.get("name", ""),
                "id": block.get("id", ""),
                "json_buf": "",
            }
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                chunk = delta.get("text", "")
                if chunk:
                    self.text_parts.append(chunk)
                    out.append({"type": "text_delta", "text": chunk})
            elif dtype == "thinking_delta":
                chunk = delta.get("thinking", "")
                if chunk:
                    out.append({"type": "thinking_delta", "text": chunk})
            elif dtype == "input_json_delta":
                blk = self.blocks.setdefault(data.get("index", 0), {"json_buf": ""})
                blk["json_buf"] = blk.get("json_buf", "") + delta.get("partial_json", "")
        elif kind == "content_block_stop":
            blk = self.blocks.pop(data.get("index", 0), None)
            if blk and blk.get("type") == "tool_use":
                out.append({
                    "type": "tool_call",
                    "id": blk.get("id", ""),
                    "name": blk.get("name", ""),
                    "input": _loads(blk.get("json_buf") or "{}") or {},
                })
        elif kind == "message_delta":
            self.stop_reason = (
                (data.get("delta") or {}).get("stop_reason") or self.stop_reason
            )
            u = data.get("usage") or {}
            if u.get("output_tokens"):
                self.usage["output_tokens"] += u["output_tokens"]
        elif kind == "error":
            err = data.get("error") or {}
            self.error = {
                "type": "error",
                "message": err.get("message") or "vendor stream error",
                "retryable": err.get("type") in {"overloaded_error", "rate_limit_error"},
            }
            out.append(self.error)
        return out


async def _translate_anthropic(
    frames: AsyncIterator[tuple[str | None, str]],
) -> AsyncIterator[NormEvent]:
    """Anthropic / GLM Messages API streaming (named events)."""
    tracker = AnthropicDeltaTracker()

    async for name, payload in frames:
        data = _loads(payload)
        if data is None:
            continue
        for event in tracker.handle(data, name):
            yield event
        if tracker.error is not None:
            return

    yield {"type": "usage", **tracker.usage}
    yield {
        "type": "done",
        "stop_reason": tracker.stop_reason,
        "text": tracker.text,
        "session_id": None,
        "awaiting": None,
    }


async def _translate_openai(
    frames: AsyncIterator[tuple[str | None, str]],
) -> AsyncIterator[NormEvent]:
    """OpenAI Chat Completions streaming (unnamed frames, ``[DONE]`` sentinel)."""
    text_parts: list[str] = []
    calls: dict[int, dict] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    stop_reason = "stop"

    async for _name, payload in frames:
        if payload.strip() == "[DONE]":
            break
        data = _loads(payload)
        if data is None:
            continue
        if data.get("usage"):
            u = data["usage"]
            usage["input_tokens"] = u.get("prompt_tokens", 0) or 0
            usage["output_tokens"] = u.get("completion_tokens", 0) or 0
        choices = data.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        stop_reason = choice.get("finish_reason") or stop_reason
        delta = choice.get("delta") or {}
        chunk = delta.get("content")
        if chunk:
            text_parts.append(chunk)
            yield {"type": "text_delta", "text": chunk}
        for frag in delta.get("tool_calls") or []:
            idx = frag.get("index", 0)
            slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
            if frag.get("id"):
                slot["id"] = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            slot["args"] += fn.get("arguments") or ""

    for slot in calls.values():
        yield {
            "type": "tool_call",
            "id": slot["id"],
            "name": slot["name"],
            "input": _loads(slot["args"] or "{}") or {},
        }
    yield {"type": "usage", **usage}
    yield {
        "type": "done",
        "stop_reason": stop_reason,
        "text": "".join(text_parts),
        "session_id": None,
        "awaiting": None,
    }


async def _translate_google(
    frames: AsyncIterator[tuple[str | None, str]],
) -> AsyncIterator[NormEvent]:
    """Gemini ``streamGenerateContent?alt=sse`` (unnamed frames, no sentinel)."""
    text_parts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    stop_reason = "STOP"

    async for _name, payload in frames:
        data = _loads(payload)
        if data is None:
            continue
        meta = data.get("usageMetadata") or {}
        if meta:
            usage["input_tokens"] = meta.get("promptTokenCount", 0) or 0
            usage["output_tokens"] = meta.get("candidatesTokenCount", 0) or 0
        for cand in data.get("candidates") or []:
            stop_reason = cand.get("finishReason") or stop_reason
            for part in (cand.get("content") or {}).get("parts") or []:
                if not isinstance(part, dict):
                    continue
                call = part.get("functionCall")
                if call:
                    yield {
                        "type": "tool_call",
                        "id": call.get("name", ""),
                        "name": call.get("name", ""),
                        "input": call.get("args") or {},
                    }
                    continue
                chunk = part.get("text") or ""
                if not chunk:
                    continue
                # Thinking parts carry no answer text — mirror _parse_google.
                if part.get("thought"):
                    yield {"type": "thinking_delta", "text": chunk}
                    continue
                text_parts.append(chunk)
                yield {"type": "text_delta", "text": chunk}

    # Gemini has no [DONE] sentinel — the stream simply ends.
    yield {"type": "usage", **usage}
    yield {
        "type": "done",
        "stop_reason": stop_reason,
        "text": "".join(text_parts),
        "session_id": None,
        "awaiting": None,
    }


_TRANSLATORS = {
    "anthropic": _translate_anthropic,
    "glm": _translate_anthropic,  # Anthropic-compatible wire format
    "openai": _translate_openai,
    "google": _translate_google,
}


# ── Public API ───────────────────────────────────────────────────────────────

async def stream_chat(
    *,
    vendor: str,
    model: str,
    messages: list[ChatMessage],
    system: str | None = None,
    max_tokens: int = 4096,
    timeout: float = 300.0,
    tools: list[dict] | None = None,
) -> AsyncIterator[NormEvent]:
    """Stream a chat completion, yielding normalised events.

    Always terminates with either an ``error`` event or a ``done`` event; never
    raises for a vendor-side failure, because a chat panel needs a reason on
    screen rather than a 500 behind the stream.

    Note the timeout is httpx's *per-read* timeout, not a total budget — which
    is what a stream wants: a long pause while the model thinks is fine, a
    stalled connection is not.
    """
    translator = _TRANSLATORS.get(vendor)
    if translator is None:
        yield {"type": "error", "message": f"Unknown system LLM vendor: {vendor!r}",
               "retryable": False}
        return
    try:
        api_key = resolve_api_key(vendor)
    except ValueError as exc:
        yield {"type": "error", "message": str(exc), "retryable": False}
        return

    url, headers, body = _BUILDERS[vendor](
        api_key, model, messages, max_tokens, False,
        system=system, stream=True, tools=tools,
    )

    yield {"type": "start", "vendor": vendor, "model": model, "transport": "http"}
    logger.info(
        "Assistant stream: vendor=%s model=%s turns=%d", vendor, model, len(messages)
    )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0)
        ) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    # Inside client.stream(), resp.text raises ResponseNotRead —
                    # the body has to be pulled explicitly first.
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    yield {
                        "type": "error",
                        "message": f"{vendor} API error {resp.status_code}: {detail}",
                        "retryable": resp.status_code in _RETRYABLE,
                    }
                    return
                async for event in translator(_iter_sse(resp)):
                    yield event
    except httpx.HTTPError as exc:
        logger.warning("Assistant stream transport failure (%s): %s", vendor, exc)
        yield {
            "type": "error",
            "message": f"{vendor} connection failed: {exc}",
            "retryable": True,
        }
