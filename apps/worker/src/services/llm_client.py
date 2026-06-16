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
        # Google Generative AI REST endpoint. The model name is
        # interpolated into the URL by the caller.
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "api_key_attr": "gemini_api_key",
        "format": "google",
    },
}


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_anthropic_request(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    json_mode: bool = False,
) -> tuple[str, dict, dict]:
    """Return (url, headers, json_body) for Anthropic-format APIs.

    Anthropic has no dedicated JSON-output flag, so ``json_mode`` is a no-op
    here — callers rely on the prompt + robust parsing for these vendors.
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
        "messages": [{"role": "user", "content": prompt}],
    }
    return url, headers, body


def _build_glm_request(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    json_mode: bool = False,
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
        "messages": [{"role": "user", "content": prompt}],
    }
    # Enable thinking for GLM-5.x models (per docs.z.ai/guides/overview/migrate-to-glm-new)
    if model.startswith("glm-5"):
        body["thinking"] = {"type": "enabled"}
    return url, headers, body


def _build_openai_request(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    json_mode: bool = False,
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
    # Azure Foundry requires max_completion_tokens for newer models
    # (gpt-4o, gpt-4.1, gpt-5.x), while standard OpenAI uses max_tokens
    if base_url:
        body = {
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": max_tokens,
        }
    else:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    # Standard OpenAI / Azure both support JSON output mode for chat models.
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return url, headers, body


def _build_google_request(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    json_mode: bool = False,
) -> tuple[str, dict, dict]:
    """Google Generative AI REST format."""
    base = _VENDOR_CONFIGS["google"]["url"]
    url = f"{base.format(model=model)}?key={api_key}"
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
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
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
    from services import agent_backends

    backend = agent_backends.get_backend(vendor)
    cmd = backend.build_command(
        prompt=prompt,
        model=model,
        allowed_tools="",
        session_id=None,
        max_turns=None,
    )
    # billing_mode='max' strips the vendor's API key (and, for Gemini, the
    # Vertex/ADC env) so the CLI uses the subscription OAuth login.
    env = backend.build_env(billing_mode="max")
    timeout_seconds = max(timeout, _CLI_MIN_TIMEOUT_SECONDS)

    logger.info(
        "System LLM call (subscription/CLI): vendor=%s model=%s prompt_len=%d",
        vendor, model, len(prompt),
    )

    with tempfile.TemporaryDirectory(prefix="devserver-syscli-") as workdir:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ValueError(
                f"{backend.label} CLI (subscription mode) timed out "
                f"after {timeout_seconds}s"
            )

    exit_code = proc.returncode or 0
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

    cfg = _VENDOR_CONFIGS[vendor]
    api_key_attr = cfg["api_key_attr"]
    api_key = getattr(settings, api_key_attr, "") or ""
    if not api_key:
        raise ValueError(
            f"System LLM vendor is {vendor!r} but {api_key_attr.upper()} "
            f"is not set in .env"
        )

    builder = _BUILDERS[vendor]
    url, headers, body = builder(api_key, model, prompt, max_tokens, json_mode)

    logger.info(
        "System LLM call: vendor=%s model=%s prompt_len=%d",
        vendor, model, len(prompt),
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
