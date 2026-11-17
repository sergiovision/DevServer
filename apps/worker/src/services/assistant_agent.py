"""Agentic engine for the Ask Agent panel.

Runs the vendor's coding-agent CLI with DevServer's MCP server attached and
translates its NDJSON stdout into the same normalised event stream the chat
HTTP path produces, so the browser renders both identically.

**Why the CLI rather than a Python tool-loop.** DevServer's whole billing model
is subscription mode: the CLI is already authenticated against the operator's
Claude Max / ChatGPT Plus / Google AI Pro login, and there is no way to drive
an OAuth subscription session over raw HTTP. A Python loop would force every
assistant question onto a metered API key. The CLI also brings tool execution,
output truncation and session resume for free.

**The cost of that choice**, stated plainly: only Claude Code emits
``stream-json``. ``agy`` has no structured output at all and codex's JSONL is
only partly structured, so this engine is anthropic/glm-only and the router in
``routes/assistant.py`` falls back to chat mode for the others.

The wire format below was captured from a real run rather than taken from
documentation — see ``AnthropicDeltaTracker`` for the part that is shared with
the HTTP path.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from services import agent_backends, agent_cli_installer, assistant_mcp
from services import proc as proc_util
from services.llm_stream import AnthropicDeltaTracker, NormEvent
from services.secret_rules import redact_secrets

logger = logging.getLogger(__name__)

__all__ = ["AGENT_VENDORS", "stream_agent_turn"]

AGENT_VENDORS = {"anthropic", "glm"}

_MAX_TURNS = 24
_TIMEOUT_SECONDS = 900
#: A `Read` tool_result can be megabytes. The browser gets a gist, not the file.
_PREVIEW_CHARS = 1500
_STDERR_KEEP = 12


def _session_workdir(session_id: str) -> Path:
    """A stable directory per conversation, so ``--resume`` finds its context."""
    from config import settings

    path = Path(settings.log_dir) / "assistant" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _resolve_repo_root(repo_id: int | None) -> str | None:
    """Where the assistant should look when the operator is asking about a repo.

    Read-only by construction: the tool policy removes Write/Edit/Bash, so
    pointing the CLI at a real checkout lets it answer about actual code without
    being able to touch it. Any failure (no database, unknown repo) degrades to
    the scratch directory rather than aborting the turn.
    """
    if repo_id is None:
        return None
    try:
        from sqlalchemy import text as sql_text

        from models.base import async_session
        from services import git_ops

        async with async_session() as db:
            row = (
                (
                    await db.execute(
                        sql_text("SELECT provider, gitea_url FROM repos WHERE id = :i"),
                        {"i": repo_id},
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            return None
        if (row.get("provider") or "") == "local":
            root = git_ops.resolve_local_root(row.get("gitea_url") or "")
            return root if root and Path(root).is_dir() else None
    except Exception as exc:
        logger.debug("Assistant repo root lookup failed for %s: %s", repo_id, exc)
    return None


def _preview(content: object) -> str:
    """Flatten a tool_result payload into a short human-readable string."""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    elif isinstance(content, (dict, list)):
        text = json.dumps(content)[:_PREVIEW_CHARS]
    else:
        text = str(content)
    text = text.strip()
    text = text[:_PREVIEW_CHARS] + ("…" if len(text) > _PREVIEW_CHARS else "")
    return _redact(text)


def _redact(text: str) -> str:
    """Screen streamed text with the shared secret rules before it leaves the worker."""
    return redact_secrets(text)[0] if text else text


async def stream_agent_turn(
    *,
    session,
    user_text: str,
    system_prompt: str,
    vendor: str,
    model: str,
    billing_mode: str = "max",
    repo_id: int | None = None,
    resume: bool = True,
    max_turns: int = _MAX_TURNS,
    timeout: float = _TIMEOUT_SECONDS,
) -> AsyncIterator[NormEvent]:
    """Run one agentic turn, yielding normalised events as they happen.

    ``session`` is the route's in-memory session object; this function reads and
    updates ``session.cli_session_id`` so the next turn resumes the same
    conversation instead of re-sending the transcript.
    """
    backend = agent_backends.get_backend(vendor)
    # First Ask Agent turn on a fresh deployment fetches the CLI (~60–90 s).
    # The notice keeps the panel honest about why the first reply is slow;
    # subsequent turns find the binary already in the install volume.
    if not backend.is_available():
        yield {
            "type": "notice",
            "text": f"Installing the {backend.label} CLI — this happens once.",
        }
    if not await agent_cli_installer.ensure_cli(backend):
        yield {
            "type": "error",
            "message": backend.not_installed_message(),
            "retryable": False,
        }
        return

    workdir = _session_workdir(session.id)
    repo_root = await _resolve_repo_root(repo_id if repo_id is not None else session.repo_id)
    cwd = repo_root or str(workdir)

    # The MCP config lives in the session dir even when the CLI runs inside a
    # repo — never write DevServer plumbing into the operator's checkout.
    if not session.mcp_config_path:
        session.mcp_config_path = assistant_mcp.write_mcp_config(
            str(workdir),
            repo_id=repo_id if repo_id is not None else session.repo_id,
            session_id=session.id,
            agent_label="assistant",
        )
    session.workdir = cwd

    resuming = bool(resume and session.cli_session_id)
    # The system prompt carries the handbook and the operator's current screen.
    # On a resumed turn the CLI already has it in conversation history, so
    # re-sending it would burn thousands of tokens per turn for nothing.
    prompt = user_text if resuming else f"{system_prompt}\n\n---\n\n{user_text}"

    cmd = backend.build_command(
        prompt=prompt,
        # A floating alias (`claude-opus-latest`) becomes a concrete model id.
        model=agent_backends.resolve_model(vendor, model),
        allowed_tools=assistant_mcp.ASSISTANT_ALLOWED_TOOLS,
        session_id=session.cli_session_id if resuming else None,
        max_turns=max_turns,
        mcp_config_path=session.mcp_config_path,
        stream_json=True,
        extra_args=assistant_mcp.ASSISTANT_EXTRA_ARGS,
    )
    env = backend.build_env(billing_mode=billing_mode)
    stdin_payload = backend.stdin_payload(prompt)

    yield {"type": "start", "vendor": vendor, "model": model, "transport": "cli"}
    logger.info(
        "Assistant agent turn: vendor=%s model=%s cwd=%s resume=%s",
        vendor,
        model,
        cwd,
        resuming,
    )

    tracker = AnthropicDeltaTracker()
    # The CLI reports each tool call twice: once through the forwarded
    # `stream_event` deltas and again in the complete `assistant` message that
    # follows. Emitting both would double every entry in the panel's tool
    # timeline, so the first one wins and the id is remembered.
    seen_tool_ids: set[str] = set()
    tool_names: dict[str, str] = {}
    stderr_tail: list[str] = []
    saw_result = False
    stop_reason = "end_turn"
    cost_usd = 0.0
    awaiting: str | None = None
    unparsed = 0

    try:
        async for channel, line in proc_util.run_streamed(
            cmd,
            cwd=cwd,
            env=env,
            timeout=timeout,
            # Claude/GLM take the prompt on stdin on Windows; elsewhere this is
            # None and the prompt is already in argv. The CLI warns after 3s if
            # stdin stays open with nothing on it, so always close it.
            stdin_input=stdin_payload or b"",
        ):
            if channel == "stderr":
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-_STDERR_KEEP]
                continue
            if channel == "exit":
                if not saw_result:
                    detail = " ".join(s for s in stderr_tail if s.strip())[:400]
                    yield {
                        "type": "error",
                        "message": (
                            f"The {backend.label} CLI exited ({line}) without "
                            f"answering. {detail}".strip()
                        ),
                        "retryable": True,
                    }
                continue

            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                unparsed += 1
                continue
            if not isinstance(obj, dict):
                continue

            kind = obj.get("type")

            if kind == "system":
                if obj.get("subtype") == "init":
                    if obj.get("session_id"):
                        session.cli_session_id = obj["session_id"]
                    for server in obj.get("mcp_servers") or []:
                        status = (server or {}).get("status")
                        if status and status not in ("connected", "ok"):
                            yield {
                                "type": "notice",
                                "text": (
                                    f"DevServer tools unavailable "
                                    f"({server.get('name', 'MCP')}: {status}) — "
                                    "answering without them."
                                ),
                            }
                continue

            if kind == "stream_event":
                # The CLI forwards genuine Anthropic streaming events verbatim,
                # so the HTTP path's tracker handles them unchanged.
                for event in tracker.handle(obj.get("event") or {}):
                    if event["type"] == "tool_call":
                        if event["id"] in seen_tool_ids:
                            continue
                        seen_tool_ids.add(event["id"])
                        tool_names[event["id"]] = event["name"]
                    elif event["type"] == "text_delta":
                        event = {**event, "text": _redact(event.get("text") or "")}
                    yield event
                continue

            if kind == "assistant":
                for block in (obj.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = block.get("id", "")
                        if tool_id in seen_tool_ids:
                            continue
                        seen_tool_ids.add(tool_id)
                        tool_names[tool_id] = block.get("name", "")
                        yield {
                            "type": "tool_call",
                            "id": tool_id,
                            "name": block.get("name", ""),
                            "input": block.get("input") or {},
                        }
                continue

            if kind == "user":
                for block in (obj.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        preview = _preview(block.get("content"))
                        tool_id = block.get("tool_use_id", "")
                        yield {
                            "type": "tool_result",
                            "id": tool_id,
                            "name": tool_names.get(tool_id, ""),
                            "ok": not block.get("is_error"),
                            "preview": preview,
                        }
                        # propose_action returns an id and then the agent stops;
                        # surface it as the card the operator clicks.
                        action = _extract_action(block.get("content"))
                        if action:
                            awaiting = f"action:{action['action_id']}"
                            yield {"type": "action_proposed", **action}
                continue

            if kind == "result":
                saw_result = True
                stop_reason = obj.get("stop_reason") or obj.get("subtype") or stop_reason
                cost_usd = float(obj.get("total_cost_usd") or 0)
                if obj.get("session_id"):
                    session.cli_session_id = obj["session_id"]
                if obj.get("is_error"):
                    yield {
                        "type": "error",
                        "message": str(obj.get("result") or "the agent reported an error"),
                        "retryable": obj.get("subtype") == "error_max_turns",
                    }
                elif not tracker.text.strip() and obj.get("result"):
                    # Non-streaming fallback: some turns only report at the end.
                    tracker.text_parts.append(str(obj["result"]))
                    yield {"type": "text_delta", "text": _redact(str(obj["result"]))}
                usage = obj.get("usage") or {}
                yield {
                    "type": "usage",
                    "input_tokens": usage.get("input_tokens", 0) or 0,
                    "output_tokens": usage.get("output_tokens", 0) or 0,
                    "cost_usd": cost_usd,
                }
                continue
    except TimeoutError:
        yield {
            "type": "error",
            "message": f"The assistant timed out after {int(timeout)}s.",
            "retryable": True,
        }
    except FileNotFoundError:
        yield {
            "type": "error",
            "message": backend.not_installed_message(),
            "retryable": False,
        }
    except Exception as exc:
        logger.exception("Assistant agent turn failed")
        yield {"type": "error", "message": str(exc), "retryable": False}

    if unparsed:
        logger.debug("Assistant agent: %d unparseable stdout lines", unparsed)

    yield {
        "type": "done",
        "stop_reason": stop_reason,
        "text": tracker.text,
        "session_id": session.cli_session_id,
        "awaiting": awaiting,
    }


def _extract_action(content: object) -> dict | None:
    """Pull a pending-approval proposal out of a ``propose_action`` result.

    The payload arrives double-encoded: our MCP tool returns a JSON *string*,
    and FastMCP then wraps that in ``{"result": "<json string>"}``. So unwrap
    up to two layers, and tolerate either shape in case the envelope changes.
    """
    texts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
    elif isinstance(content, str):
        texts.append(content)
    elif isinstance(content, dict):
        texts.append(json.dumps(content))

    for text in texts:
        data: object = text
        for _ in range(3):
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    break
            elif isinstance(data, dict) and "result" in data and "status" not in data:
                data = data["result"]
            else:
                break
        if isinstance(data, dict) and data.get("status") == "awaiting_approval":
            return {
                "action_id": str(data.get("action_id") or ""),
                "kind": str(data.get("kind") or "action"),
                "summary": str(data.get("summary") or ""),
                "params": data.get("params") or {},
                "risk": str(data.get("risk") or "medium"),
            }
    return None
