"""Vendor-agnostic LLM API client for system tasks.

System tasks like "Fill Task" (devtask skill) need a quick LLM call —
just text-in, text-out, no tool-use, no file I/O. They don't go through
the agent CLI pipeline and don't need ``AgentBackend``.

This module wraps ``httpx`` calls to whichever vendor is configured in
the ``system_llm_vendor`` / ``system_llm_model`` settings (editable on
the /settings page). Each vendor has a different API shape:

    - **anthropic** — Anthropic Messages API (``api.anthropic.com``)
    - **glm**       — Zhipu BigModel API, Anthropic-compatible
                      (``open.bigmodel.cn/api/anthropic``)
    - **openai**    — OpenAI Chat Completions API
    - **google**    — Google Generative AI REST API

The caller just passes a prompt string and gets a response string back.
All vendor-specific auth, headers, request shape, and response parsing
live here.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile

import httpx

from config import settings

logger = logging.getLogger(__name__)

# ── Vendor configs ────────────────────────────────────────────────────────────

_VENDOR_CONFIGS: dict[str, dict] = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "api_key_attr": "anthropic_api_key",
        "format": "anthropic",
    },
    "glm": {
        # Zhipu's Anthropic-compatible endpoint — same request/response
        # shape as Anthropic, different base URL and auth key.
        "url": "https://open.bigmodel.cn/api/anthropic/v1/messages",
        "api_key_attr": "glm_api_key",
        "format": "anthropic",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "api_key_attr": "openai_api_key",
        "format": "openai",
    },
    "google": {
        # Google Generative AI REST endpoint. The model name *and the method*
        # are interpolated by the builder — Gemini switches the URL, not a body
        # flag, to stream (``generateContent`` → ``streamGenerateContent``).
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}",
        "api_key_attr": "gemini_api_key",
        "format": "google",
    },
}


# ── Conversation helpers ──────────────────────────────────────────────────────
#
# Every builder below takes a *message list* rather than a single prompt string,
# so the same code serves both the one-shot system calls (Fill Task, DevPlan,
# memory rerank) and the multi-turn Ask Agent panel. ``complete()`` keeps its
# original prompt-string signature by wrapping the prompt in a one-element list.

ChatMessage = dict  # {"role": "user" | "assistant", "content": str}


def _normalize_alternating(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Merge consecutive same-role turns and drop empties.

    The Anthropic Messages API rejects a transcript whose roles do not strictly
    alternate (``400 messages: roles must alternate``). A chat UI produces such
    transcripts easily — e.g. two user messages in a row when the first turn
    errored — so normalise instead of letting the vendor 400.
    """
    out: list[ChatMessage] = []
    for msg in messages:
        role = "assistant" if msg.get("role") == "assistant" else "user"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] = f"{out[-1]['content']}\n\n{content}"
        else:
            out.append({"role": role, "content": content})
    # Anthropic also requires the transcript to start with a user turn.
    while out and out[0]["role"] == "assistant":
        out.pop(0)
    return out or [{"role": "user", "content": "(empty)"}]


def _flatten_transcript(messages: list[ChatMessage], system: str | None) -> str:
    """Render a transcript as one prompt string for the CLI (subscription) path.

    The agent CLIs take a single ``-p`` prompt, so a multi-turn conversation has
    to be flattened. This is lossy — the CLI sees a transcript it did not
    produce — and is only used as a fallback; the Ask Agent panel's agentic path
    uses the CLI's own ``--resume`` for real multi-turn continuity.
    """
    parts: list[str] = []
    if system:
        parts.append(system.strip())
    for msg in messages:
        label = "Assistant" if msg.get("role") == "assistant" else "User"
        parts.append(f"{label}: {(msg.get('content') or '').strip()}")
    parts.append("Assistant:")
    return "\n\n".join(p for p in parts if p)


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_anthropic_request(
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    max_tokens: int,
    json_mode: bool = False,
    *,
    system: str | None = None,
    stream: bool = False,
    tools: list[dict] | None = None,
) -> tuple[str, dict, dict]:
    """Return (url, headers, json_body) for Anthropic-format APIs.

    Anthropic has no dedicated JSON-output flag, so ``json_mode`` is a no-op
    here — callers rely on the prompt + robust parsing for these vendors. The
    system prompt is a **top-level body field**, not a message with
    ``role: 'system'`` (that shape is an OpenAI-ism and Anthropic rejects it).
    """
    vendor_cfg = _VENDOR_CONFIGS.get("anthropic", {})
    url = vendor_cfg["url"]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": _normalize_alternating(messages),
    }
    if system:
        body["system"] = system
    if stream:
        body["stream"] = True
    if tools:
        body["tools"] = tools
    return url, headers, body


def _build_glm_request(
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    max_tokens: int,
    json_mode: bool = False,
    *,
    system: str | None = None,
    stream: bool = False,
    tools: list[dict] | None = None,
) -> tuple[str, dict, dict]:
    """GLM uses Anthropic-compatible format, different URL + key.

    ``json_mode`` is a no-op (Anthropic-shaped API, no JSON flag).
    """
    url = _VENDOR_CONFIGS["glm"]["url"]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": _normalize_alternating(messages),
    }
    if system:
        body["system"] = system
    if stream:
        body["stream"] = True
    if tools:
        body["tools"] = tools
    # Enable thinking for GLM-5.x models (per docs.z.ai/guides/overview/migrate-to-glm-new)
    if model.startswith("glm-5"):
        body["thinking"] = {"type": "enabled"}
        # Thinking tokens come out of the same output budget, and GLM-5.1 will
        # happily spend *all* of a small one before writing a single word of the
        # answer (observed: max_tokens=64 → 64 thinking tokens, stop_reason
        # max_tokens, empty text). Same failure mode, and same fix, as the
        # Gemini floor in _build_google_request.
        body["max_tokens"] = max(max_tokens, 4096)
    return url, headers, body


def _openai_uses_completion_tokens(model: str) -> bool:
    """True when the model rejects ``max_tokens`` in favour of ``max_completion_tokens``.

    OpenAI's reasoning-era models (o-series, gpt-5.x, gpt-6.x) 400 on ``max_tokens``;
    older chat models only understand ``max_tokens``. Sniff the model name
    rather than picking one and breaking the other half of the range.
    """
    m = (model or "").lower()
    return m.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def _build_openai_request(
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    max_tokens: int,
    json_mode: bool = False,
    *,
    system: str | None = None,
    stream: bool = False,
    tools: list[dict] | None = None,
) -> tuple[str, dict, dict]:
    """OpenAI Chat Completions format. Supports Azure Foundry via OPENAI_BASE_URL."""
    # Check for Azure Foundry / custom endpoint override
    base_url = (settings.openai_base_url or "").rstrip("/")
    if base_url:
        # Azure Foundry: construct URL with api-version query param
        api_version = settings.openai_api_version or "2024-10-01-preview"
        # Ensure /openai suffix for Azure (agent_backends.py does the same)
        if not base_url.endswith("/openai"):
            base_url = f"{base_url}/openai"
        url = f"{base_url}/deployments/{model}/chat/completions?api-version={api_version}"
        # Azure uses api-key header instead of Bearer token
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
    else:
        # Standard OpenAI
        url = _VENDOR_CONFIGS["openai"]["url"]
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    # Chat Completions has no separate system field — instructions are the
    # first message. Reasoning-era models use the newer ``developer`` role;
    # keep ``system`` for older models that predate it.
    chat: list[dict] = []
    if system:
        instruction_role = (
            "developer" if base_url or _openai_uses_completion_tokens(model) else "system"
        )
        chat.append({"role": instruction_role, "content": system})
    chat.extend(
        {"role": m.get("role", "user"), "content": m.get("content", "")}
        for m in messages
    )
    # Azure Foundry requires max_completion_tokens for newer models
    # (gpt-4o, gpt-4.1, gpt-5.x), while older standard OpenAI models use
    # max_tokens — and gpt-5.x/o-series reject it outright.
    if base_url:
        body = {"messages": chat, "max_completion_tokens": max_tokens}
    elif _openai_uses_completion_tokens(model):
        body = {"model": model, "messages": chat, "max_completion_tokens": max_tokens}
    else:
        body = {"model": model, "messages": chat, "max_tokens": max_tokens}
    # Standard OpenAI / Azure both support JSON output mode for chat models.
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if stream:
        body["stream"] = True
        # Without this the final chunk carries no token counts at all.
        body["stream_options"] = {"include_usage": True}
    if tools:
        body["tools"] = tools
    return url, headers, body


def _build_google_request(
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    max_tokens: int,
    json_mode: bool = False,
    *,
    system: str | None = None,
    stream: bool = False,
    tools: list[dict] | None = None,
) -> tuple[str, dict, dict]:
    """Google Generative AI REST format.

    Two Gemini-specific traps handled here: the assistant role on the wire is
    ``"model"`` (not ``"assistant"``), and streaming changes the **URL method**
    — ``:streamGenerateContent`` plus a mandatory ``alt=sse``, without which the
    response is one big JSON array rather than an SSE stream.
    """
    base = _VENDOR_CONFIGS["google"]["url"]
    method = "streamGenerateContent" if stream else "generateContent"
    url = f"{base.format(model=model, method=method)}?key={api_key}"
    if stream:
        url += "&alt=sse"
    headers = {"Content-Type": "application/json"}
    gen_cfg: dict = {
        # Gemini 2.5+/3 spend output tokens on internal "thinking"; a tight
        # cap leaves no room for the actual answer (finishReason MAX_TOKENS →
        # empty/truncated text → JSON parse fails). Give a generous floor.
        "maxOutputTokens": max(max_tokens, 4096),
    }
    if json_mode:
        # Force structured output: the model returns bare JSON with no markdown
        # fences or prose preamble — exactly what the JSON callers expect.
        gen_cfg["responseMimeType"] = "application/json"
    body: dict = {
        "contents": [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": m.get("content", "")}],
            }
            for m in messages
        ],
        "generationConfig": gen_cfg,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
    return url, headers, body


_BUILDERS = {
    "anthropic": _build_anthropic_request,
    "glm": _build_glm_request,
    "openai": _build_openai_request,
    "google": _build_google_request,
}


# ── Response parsers ──────────────────────────────────────────────────────────

def _parse_anthropic(data: dict) -> str:
    """Extract text from Anthropic / GLM Messages API response."""
    content = data.get("content") or []
    # content is a list of blocks; find the first text block
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    # Fallback: legacy shape
    if content and isinstance(content[0], dict):
        return content[0].get("text", "")
    return ""


def _parse_openai(data: dict) -> str:
    """Extract text from OpenAI Chat Completions response."""
    choices = data.get("choices") or []
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _parse_google(data: dict) -> str:
    """Extract text from Google Generative AI response.

    Gemini 2.5+/3 can split the answer across multiple ``parts`` and may emit
    ``thought`` parts that carry no usable text. Concatenate every real text
    part (skipping thoughts) instead of blindly taking ``parts[0]`` — which on
    a thinking model is often empty and was the source of the JSON-parse error.
    """
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", []) or []
    texts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("text") and not p.get("thought")
    ]
    return "".join(texts)


_PARSERS = {
    "anthropic": _parse_anthropic,
    "glm": _parse_anthropic,  # GLM uses Anthropic-compatible response shape
    "openai": _parse_openai,
    "google": _parse_google,
}


# ── Subscription (CLI) path ─────────────────────────────────────────────────
#
# ``mode='max'`` runs the system call through the vendor's coding-agent CLI
# (claude / codex / gemini / glm) with the API-key env var stripped, so the
# CLI authenticates against the operator's OAuth / subscription login instead
# of metering against an API key — the same mechanism agent tasks use for
# ``claude_mode='max'``. This is what lets Claude / OpenAI / Google system
# LLMs run under a flat-fee subscription rather than per-token API billing.

# The CLI is slower to cold-start than a raw HTTP call, so give it a floor on
# top of the caller's (HTTP-tuned) timeout.
_CLI_MIN_TIMEOUT_SECONDS = 180


async def _complete_via_cli(
    *,
    vendor: str,
    model: str,
    prompt: str,
    timeout: int,
) -> str:
    """Run a system completion through the vendor CLI in subscription mode.

    Reuses :mod:`services.agent_backends` so the command shape, env stripping
    and output parsing match exactly what agent tasks use. The CLI runs in a
    throwaway temp directory (no repo context, no file side effects) and the
    API-key env var is stripped via ``build_env(billing_mode='max')`` so the
    CLI falls back to its OAuth / subscription session.
    """
    from services import agent_backends, agent_cli_installer
    from services import proc as proc_util

    backend = agent_backends.get_backend(vendor)
    # The image ships no vendor CLIs — fetch this one on first use. No task
    # context here, so no events: the install is logged and, if it fails, the
    # existing not-installed message explains the fix.
    if not await agent_cli_installer.ensure_cli(backend):
        raise ValueError(backend.not_installed_message())
    cmd = backend.build_command(
        prompt=prompt,
        model=model,
        allowed_tools="",
        session_id=None,
        max_turns=None,
    )
    # Claude/GLM deliver the prompt on stdin (avoids cmd.exe's command-line
    # length limit on Windows); other vendors pass it via argv.
    stdin_payload = backend.stdin_payload(prompt)
    # billing_mode='max' strips the vendor's API key (and, for Gemini, the
    # Vertex/ADC env) so the CLI uses the subscription OAuth login.
    env = backend.build_env(billing_mode="max")
    timeout_seconds = max(timeout, _CLI_MIN_TIMEOUT_SECONDS)

    logger.info(
        "System LLM call (subscription/CLI): vendor=%s model=%s prompt_len=%d",
        vendor, model, len(prompt),
    )

    with tempfile.TemporaryDirectory(prefix="devserver-syscli-") as workdir:
        try:
            exit_code, stdout_data, stderr_data = await proc_util.run(
                cmd,
                cwd=workdir,
                env=env,
                timeout=timeout_seconds,
                stdin_devnull=stdin_payload is None,
                stdin_input=stdin_payload,
            )
        except FileNotFoundError:
            raise ValueError(backend.not_installed_message())
        except asyncio.TimeoutError:
            raise ValueError(
                f"{backend.label} CLI (subscription mode) timed out "
                f"after {timeout_seconds}s"
            )

    stdout_text = stdout_data.decode(errors="replace")
    stderr_text = stderr_data.decode(errors="replace")

    if exit_code != 0:
        raise ValueError(
            f"{backend.label} CLI (subscription mode) exited {exit_code}: "
            f"{(stderr_text or stdout_text)[:500]}"
        )

    parsed = backend.parse_output(stdout_text, None)
    if parsed.error:
        raise ValueError(
            f"{backend.label} CLI (subscription mode) error: {parsed.error}"
        )
    text = parsed.result or ""
    if not text:
        logger.warning(
            "System LLM (subscription/CLI) returned empty text for vendor=%s",
            vendor,
        )
    return text


# ── Public API ────────────────────────────────────────────────────────────────

async def complete(
    *,
    vendor: str,
    model: str,
    prompt: str,
    max_tokens: int = 1024,
    timeout: int = 60,
    json_mode: bool = False,
    mode: str = "max",
) -> str:
    """Call the configured LLM vendor and return the response text.

    ``mode`` selects the billing/transport path (mirrors the per-task
    ``claude_mode``):

    - ``'max'``  — **subscription**. Run the vendor coding-agent CLI with its
                   API key stripped so it uses the operator's OAuth /
                   subscription login. No per-token API metering.
    - ``'api'``  — **API platform**. Direct HTTP call against the vendor API
                   using the configured API key (the original behaviour).

    Set ``json_mode=True`` when the response must be a single JSON object —
    for Google (Gemini) this sets ``responseMimeType: application/json`` and
    for OpenAI ``response_format: json_object``, which suppresses markdown
    fences / prose preamble that otherwise break ``json.loads``. ``json_mode``
    only affects the API path; the CLI path relies on the prompt + robust
    JSON extraction the callers already perform.

    Raises ``ValueError`` on missing API key, unknown vendor, or CLI failure.
    Raises ``httpx.HTTPStatusError`` (or similar) on API errors —
    callers should catch and surface a user-friendly message.
    """
    if vendor not in _BUILDERS:
        raise ValueError(f"Unknown system LLM vendor: {vendor!r}")

    # Subscription mode runs through the vendor CLI's OAuth login. GLM is the
    # exception: the ``glm`` launcher IS Claude Code CLI redirected to Zhipu's
    # API and *needs* GLM_API_KEY — it has no separate OAuth subscription — so
    # GLM always uses the HTTP path (this also keeps the default glm/max
    # deployment working out of the box).
    if mode == "max" and vendor != "glm":
        return await _complete_via_cli(
            vendor=vendor, model=model, prompt=prompt, timeout=timeout,
        )

    return await _complete_via_http(
        vendor=vendor,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        system=None,
        max_tokens=max_tokens,
        timeout=timeout,
        json_mode=json_mode,
    )


async def complete_chat(
    *,
    vendor: str,
    model: str,
    messages: list[ChatMessage],
    system: str | None = None,
    max_tokens: int = 4096,
    timeout: int = 120,
    json_mode: bool = False,
    mode: str = "api",
) -> str:
    """Multi-turn sibling of :func:`complete` — a transcript plus a system prompt.

    Same vendors, same auth, same billing modes; the difference is that the
    caller supplies a ``[{"role": ..., "content": ...}]`` list and an optional
    system prompt instead of one prompt string. This is what the Ask Agent panel
    uses for its non-agentic (chat) turns.

    ``mode`` defaults to ``'api'`` here, not ``'max'``: the CLI path can only
    take a single prompt, so a subscription-mode chat turn is flattened into a
    fabricated transcript (see :func:`_flatten_transcript`) and loses real turn
    structure. Callers that want genuine multi-turn under a subscription should
    use the agentic path's ``--resume`` instead.
    """
    if vendor not in _BUILDERS:
        raise ValueError(f"Unknown system LLM vendor: {vendor!r}")
    if mode == "max" and vendor != "glm":
        return await _complete_via_cli(
            vendor=vendor,
            model=model,
            prompt=_flatten_transcript(messages, system),
            timeout=timeout,
        )
    return await _complete_via_http(
        vendor=vendor,
        model=model,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        timeout=timeout,
        json_mode=json_mode,
    )


async def _complete_via_http(
    *,
    vendor: str,
    model: str,
    messages: list[ChatMessage],
    system: str | None,
    max_tokens: int,
    timeout: int,
    json_mode: bool,
) -> str:
    """Shared direct-HTTP leg for :func:`complete` and :func:`complete_chat`."""
    api_key = resolve_api_key(vendor)

    builder = _BUILDERS[vendor]
    url, headers, body = builder(
        api_key, model, messages, max_tokens, json_mode, system=system,
    )

    prompt_len = sum(len(m.get("content") or "") for m in messages)
    logger.info(
        "System LLM call: vendor=%s model=%s turns=%d prompt_len=%d",
        vendor, model, len(messages), prompt_len,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise ValueError(
                f"{vendor} API error {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()

    parser = _PARSERS[vendor]
    text = parser(data)
    if not text:
        logger.warning("System LLM returned empty text for vendor=%s", vendor)
    return text


def resolve_api_key(vendor: str) -> str:
    """Return the configured API key for ``vendor`` or raise a actionable error."""
    cfg = _VENDOR_CONFIGS.get(vendor)
    if cfg is None:
        raise ValueError(f"Unknown system LLM vendor: {vendor!r}")
    api_key_attr = cfg["api_key_attr"]
    api_key = getattr(settings, api_key_attr, "") or ""
    if not api_key:
        raise ValueError(
            f"System LLM vendor is {vendor!r} but {api_key_attr.upper()} "
            f"is not set in .env"
        )
    return api_key
