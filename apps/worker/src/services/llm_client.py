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

import logging

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
        "api_key_attr": "google_api_key",
        "format": "google",
    },
}


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_anthropic_request(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> tuple[str, dict, dict]:
    """Return (url, headers, json_body) for Anthropic-format APIs."""
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
) -> tuple[str, dict, dict]:
    """GLM uses Anthropic-compatible format, different URL + key."""
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
) -> tuple[str, dict, dict]:
    """OpenAI Chat Completions format."""
    url = _VENDOR_CONFIGS["openai"]["url"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    return url, headers, body


def _build_google_request(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> tuple[str, dict, dict]:
    """Google Generative AI REST format."""
    base = _VENDOR_CONFIGS["google"]["url"]
    url = f"{base.format(model=model)}?key={api_key}"
    headers = {"Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
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
    """Extract text from Google Generative AI response."""
    candidates = data.get("candidates") or []
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            return parts[0].get("text", "")
    return ""


_PARSERS = {
    "anthropic": _parse_anthropic,
    "glm": _parse_anthropic,  # GLM uses Anthropic-compatible response shape
    "openai": _parse_openai,
    "google": _parse_google,
}


# ── Public API ────────────────────────────────────────────────────────────────

async def complete(
    *,
    vendor: str,
    model: str,
    prompt: str,
    max_tokens: int = 1024,
    timeout: int = 60,
) -> str:
    """Call the configured LLM vendor and return the response text.

    Raises ``ValueError`` on missing API key or unknown vendor.
    Raises ``httpx.HTTPStatusError`` (or similar) on API errors —
    callers should catch and surface a user-friendly message.
    """
    if vendor not in _BUILDERS:
        raise ValueError(f"Unknown system LLM vendor: {vendor!r}")

    cfg = _VENDOR_CONFIGS[vendor]
    api_key_attr = cfg["api_key_attr"]
    api_key = getattr(settings, api_key_attr, "") or ""
    if not api_key:
        raise ValueError(
            f"System LLM vendor is {vendor!r} but {api_key_attr.upper()} "
            f"is not set in .env"
        )

    builder = _BUILDERS[vendor]
    url, headers, body = builder(api_key, model, prompt, max_tokens)

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
