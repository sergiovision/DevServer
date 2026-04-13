"""Agent backend abstraction — decouples DevServer from Anthropic's CLI.

Historically ``agent_runner._run_claude`` was hard-coded to shell out to
``claude -p "..."``. That works while Anthropic is the only vendor, and
breaks the moment you want a wingman that survives an Anthropic outage,
rate limit, or policy change.

This module introduces a narrow :class:`AgentBackend` protocol that
covers everything a coding-agent CLI needs to do from DevServer's
perspective:

    1. Build the CLI command for a given (prompt, model, tools, session)
       tuple.
    2. Build the subprocess environment (per-vendor API key, or stripped
       of the API key to force an OAuth / subscription login).
    3. Detect a rate-limit failure in the combined stdout+stderr+exit_code
       shape — each vendor has a different 429 format.
    4. Parse the CLI's JSON output into a normalised :class:`AgentResult`.

The actual subprocess spawning, timeout handling, rate-limit retry loop,
and task-event emission all live in ``agent_runner._run_agent`` which
calls into the backend through this interface. That keeps vendor-specific
knowledge in one file per vendor and lets the runner stay vendor-agnostic.

Current backends:
    - :class:`ClaudeBackend` (Anthropic) — fully implemented, production tested
    - :class:`GeminiBackend` (Google)    — command shape known, untested
    - :class:`OpenAIBackend` (Codex CLI) — command shape known, untested
    - :class:`GLMBackend`    (Zhipu AI)   — wraps Claude Code CLI via ``glm`` launcher

Only ``claude`` is exercised end-to-end as of this commit. The others are
structurally complete so that adding a real fallback is a small change
(swap the CLI binary name, verify the JSON shape, done) rather than a
full refactor.

The registry is available as :data:`VENDOR_MODELS` and :data:`VENDOR_LABELS`
— the web UI reads these via a lightweight HTTP endpoint to populate the
two-step "vendor → model" combobox on the task form.
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Registry: supported vendors and their recommended models ────────────────
#
# These are the choices the task-form combobox presents. Users are still
# allowed to type a free-form model string (repo default, old model, etc.),
# so the list is a hint — not an enforced allow-list.
#
# Ordering matters: the first entry in each list is the "strong" default
# that gets auto-selected when a user switches vendor in the UI.

VENDOR_MODELS: dict[str, list[dict[str, str]]] = {
    "anthropic": [
        {"id": "claude-opus-4-6",              "label": "Claude Opus 4.6 (most capable)"},
        {"id": "claude-sonnet-4-6",            "label": "Claude Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001",    "label": "Claude Haiku 4.5"},
        {"id": "claude-opus-4-5",              "label": "Claude Opus 4.5"},
        {"id": "claude-sonnet-4-5",            "label": "Claude Sonnet 4.5"},
    ],
    "google": [
        {"id": "gemini-3-pro-preview",         "label": "Gemini 3 Pro Preview (strong coding)"},
        {"id": "gemini-3-flash-preview",       "label": "Gemini 3 Flash Preview (cheap, fast)"},
        {"id": "gemini-2.5-pro",               "label": "Gemini 2.5 Pro (stable)"},
        {"id": "gemini-pro-latest",            "label": "Gemini Pro (latest alias)"},
    ],
    "openai": [
        {"id": "gpt-5.3-codex",                "label": "GPT-5.3 Codex (coding-tuned)"},
        {"id": "gpt-5.2",                      "label": "GPT-5.2 (reasoning)"},
        {"id": "o4-mini",                      "label": "o4-mini (cheap reasoning)"},
    ],
    "glm": [
        {"id": "glm-5.1",                      "label": "GLM-5.1 (thinking, SWE-bench Pro leader, 8x cheaper)"},
        {"id": "glm-5",                        "label": "GLM-5"},
        {"id": "glm-4.7-flash",                "label": "GLM-4.7 Flash (free, zero balance ok)"},
        {"id": "glm-4.5-air",                  "label": "GLM-4.5 Air (budget)"},
    ],
}

VENDOR_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "google":    "Google",
    "openai":    "OpenAI",
    "glm":       "GLM (Zhipu)",
}

DEFAULT_VENDOR = "anthropic"


# ── Result dataclass — normalised across vendors ────────────────────────────

@dataclass
class AgentResult:
    """Normalised return shape from any backend invocation.

    Mirrors the historical dict shape that ``_run_claude`` returned, so the
    refactor inside ``agent_runner`` is a narrow replacement rather than a
    rewrite of every downstream call site. Consumers still read the same
    fields: ``result``, ``cost_usd``, ``num_turns``, ``session_id``,
    ``exit_code``, ``raw_output``, ``subtype``, ``errors``.
    """
    result: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str | None = None
    exit_code: int = 0
    raw_output: str = ""
    subtype: str = ""
    errors: list[str] = field(default_factory=list)
    error: str | None = None  # populated on timeout / hard CLI failure

    def to_dict(self) -> dict:
        """Return the legacy dict shape agent_runner already consumes."""
        return {
            "result": self.result,
            "cost_usd": self.cost_usd,
            "num_turns": self.num_turns,
            "session_id": self.session_id,
            "exit_code": self.exit_code,
            "raw_output": self.raw_output,
            "subtype": self.subtype,
            "errors": self.errors,
            **({"error": self.error} if self.error is not None else {}),
        }


# ── Backend protocol ────────────────────────────────────────────────────────

class AgentBackend(ABC):
    """Abstract interface every vendor-specific backend implements."""

    #: Short identifier matching ``tasks.agent_vendor`` values.
    vendor: str = ""
    #: Human-readable label for logs and the dashboard.
    label: str = ""
    #: Default CLI binary name on $PATH. Can be overridden per deployment
    #: via the matching setting in ``config.py`` (e.g. ``settings.claude_bin``).
    cli_bin: str = ""

    # ── Command construction ────────────────────────────────────────────
    @abstractmethod
    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,
        session_id: str | None,
        max_turns: int | None,
    ) -> list[str]:
        """Return argv for a subprocess.exec invocation."""

    #: Name of the env var this vendor uses for its API key. Subclasses
    #: override so that ``billing_mode='max'`` can strip it uniformly.
    api_key_env: str = ""

    # ── Environment ─────────────────────────────────────────────────────
    def build_env(self, billing_mode: str = "api") -> dict[str, str] | None:
        """Return the env mapping for the subprocess, or None to inherit.

        The billing mode is vendor-agnostic:

        - ``'api'``  — inherit the full environment including the
                       vendor's API-key env var. The CLI bills against
                       the key's account.
        - ``'max'``  — strip the vendor's API-key env var so the CLI
                       falls back to its own OAuth / subscription login
                       (Claude Max, ChatGPT Plus via ``codex login``,
                       Google account OAuth for Gemini, GLM stored auth).

        Subclasses only need to set ``api_key_env`` for this default
        implementation to work correctly for every vendor.
        """
        if billing_mode == "max" and self.api_key_env:
            return {k: v for k, v in os.environ.items() if k != self.api_key_env}
        return None

    # ── Rate-limit detection ────────────────────────────────────────────
    def is_rate_limit_error(self, stdout: str, stderr: str, exit_code: int) -> bool:
        """Vendor-specific 429 detector. Default: no detection."""
        if exit_code == 0:
            return False
        return False

    # ── Output parsing ──────────────────────────────────────────────────
    @abstractmethod
    def parse_output(self, raw_output: str, prior_session_id: str | None) -> AgentResult:
        """Turn the CLI's stdout into an :class:`AgentResult`."""


# ── Anthropic — Claude Code CLI ─────────────────────────────────────────────

_CLAUDE_RATE_LIMIT_RE = re.compile(
    r"rate_limit_error|rate limit of \d+\s*(?:input\s+)?tokens? per minute|429",
    re.IGNORECASE,
)


class ClaudeBackend(AgentBackend):
    """Anthropic Claude Code CLI — the original and, currently, only
    production-tested backend.

    Command shape:
        claude -p <prompt> --dangerously-skip-permissions --output-format json
               --model <model> [--max-turns N] [--allowedTools <list>]
               [--resume <session_id>]

    Billing modes:
        'max' — strip ``ANTHROPIC_API_KEY`` from the env so the CLI falls
                back to the OAuth login from ``claude login``.
        'api' — pass the full env through; the CLI uses the API key.
    """

    vendor = "anthropic"
    label = "Anthropic"
    cli_bin = "claude"
    api_key_env = "ANTHROPIC_API_KEY"

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,
        session_id: str | None,
        max_turns: int | None,
    ) -> list[str]:
        cmd = [
            self.cli_bin, "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--model", model,
        ]
        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])
        if session_id:
            cmd.extend(["--resume", session_id])
        return cmd

    def is_rate_limit_error(self, stdout: str, stderr: str, exit_code: int) -> bool:
        if exit_code == 0:
            return False
        return bool(_CLAUDE_RATE_LIMIT_RE.search(stdout)) or bool(
            _CLAUDE_RATE_LIMIT_RE.search(stderr)
        )

    def parse_output(self, raw_output: str, prior_session_id: str | None) -> AgentResult:
        res = AgentResult(
            raw_output=raw_output,
            session_id=prior_session_id,
        )
        try:
            data = json.loads(raw_output)
            res.result = data.get("result", "") or ""
            res.cost_usd = float(
                data.get("total_cost_usd") or data.get("cost_usd") or 0
            )
            res.num_turns = int(data.get("num_turns", 0) or 0)
            res.session_id = data.get("session_id", prior_session_id) or prior_session_id
            res.subtype = data.get("subtype", "") or ""
            errors = data.get("errors", [])
            if isinstance(errors, list):
                res.errors = [str(e) for e in errors]
        except (json.JSONDecodeError, TypeError, ValueError):
            res.result = raw_output
            logger.warning("Claude output was not valid JSON, using raw text")
        return res


# ── Google — Gemini CLI ─────────────────────────────────────────────────────
#
# Command shape (as documented by github.com/google-gemini/gemini-cli as of
# Q1 2026; flags subject to verification on first real run):
#     gemini -p <prompt> --model <model> --output-format json
#            [--max-turns N] [--tools <list>] [--session <id>]
#
# Auth: reads ``GEMINI_API_KEY`` from the env. On Google Cloud,
# Application Default Credentials also work.
#
# Rate-limit detection: Google Cloud surfaces 429s as either literal "429"
# in the error text or an ``Error: RESOURCE_EXHAUSTED`` line.

_GEMINI_RATE_LIMIT_RE = re.compile(
    r"RESOURCE_EXHAUSTED|429|quota.*exceeded",
    re.IGNORECASE,
)


class GeminiBackend(AgentBackend):
    """Google Gemini CLI backend.

    **Untested as of first commit.** Structurally complete so that wiring
    up a real Gemini wingman is a small follow-up rather than a refactor.
    """

    vendor = "google"
    label = "Google"
    cli_bin = "gemini"
    # Gemini CLI requires GEMINI_API_KEY in API mode. Stripped in 'max'
    # mode so the CLI falls back to the interactive Google-account OAuth
    # login it set up on first run.
    api_key_env = "GEMINI_API_KEY"

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,
        session_id: str | None,
        max_turns: int | None,
    ) -> list[str]:
        # AUTONOMOUS / HEADLESS — these two flags are mandatory. Do not remove:
        #   -p <prompt>           Forces non-interactive headless mode (without
        #                         it Gemini drops into a TUI).
        #   --approval-mode yolo  Auto-approves every tool call. The other
        #                         choices (default/auto_edit/plan) all wait
        #                         for human confirmation, which deadlocks an
        #                         unattended worker. Combined with the
        #                         stdin=DEVNULL spawn in agent_runner, the
        #                         CLI cannot block on user input.
        #
        # max_turns: Gemini CLI has no flag for this — agent_runner._run_agent
        #   writes ``<worktree>/.gemini/settings.json`` with ``model.maxSessionTurns``
        #   before spawning, which Gemini reads from cwd.
        #
        # allowed_tools: deliberately ignored. The repo's ``claude_allowed_tools``
        #   column stores Claude tool names (Read, Edit, Bash, …) which don't
        #   exist in Gemini's tool registry. Passing them via ``--allowed-tools``
        #   restricts the agent to a fictional set, so it can't call Gemini's
        #   real ``shell`` / ``write_file`` tools and burns through its turn
        #   budget retrying with the wrong names.
        cmd = [
            self.cli_bin, "-p", prompt,
            "--model", model,
            "--output-format", "json",
            "--approval-mode", "yolo",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        return cmd

    def is_rate_limit_error(self, stdout: str, stderr: str, exit_code: int) -> bool:
        if exit_code == 0:
            return False
        return bool(_GEMINI_RATE_LIMIT_RE.search(stdout)) or bool(
            _GEMINI_RATE_LIMIT_RE.search(stderr)
        )

    def parse_output(self, raw_output: str, prior_session_id: str | None) -> AgentResult:
        res = AgentResult(
            raw_output=raw_output,
            session_id=prior_session_id,
        )
        try:
            # Gemini CLI might output text like "YOLO mode is enabled..." before the JSON.
            json_start = raw_output.find("{")
            if json_start == -1:
                raise ValueError("No JSON object found in output")
            
            data = json.loads(raw_output[json_start:])
            
            response_val = data.get("response", "")
            if isinstance(response_val, dict):
                res.result = response_val.get("text", "") or ""
            else:
                res.result = response_val or data.get("result", "") or ""
                
            res.num_turns = int(data.get("turns", 0) or 0)
            res.session_id = data.get("session_id", prior_session_id) or prior_session_id
            res.subtype = data.get("subtype", "") or ""
            
            if "error" in data:
                err = data["error"]
                res.error = err.get("message") if isinstance(err, dict) else str(err)
                
        except (json.JSONDecodeError, TypeError, ValueError):
            res.result = raw_output
            logger.warning("Gemini output was not valid JSON, using raw text")
        return res


# ── OpenAI — Codex CLI ──────────────────────────────────────────────────────
#
# Command shape (openai/codex, Apache-2.0, Q1 2026):
#     codex exec <prompt> --model <model> --output-format json
#            [--max-turns N] [--resume <session_id>]
#
# Auth: reads ``OPENAI_API_KEY`` from the env, or uses the ChatGPT OAuth
# login from ``codex login``.
#
# Rate-limit detection: OpenAI returns ``rate_limit_exceeded`` in the error
# JSON or a literal 429.

_OPENAI_RATE_LIMIT_RE = re.compile(
    r"rate_limit_exceeded|429|RateLimitError",
    re.IGNORECASE,
)


class OpenAIBackend(AgentBackend):
    """OpenAI Codex CLI backend.

    **Untested as of first commit.** See GeminiBackend for the same caveat.
    """

    vendor = "openai"
    label = "OpenAI"
    cli_bin = "codex"
    # Codex CLI reads OPENAI_API_KEY. In 'max' mode we strip it so the
    # CLI falls back to the ChatGPT-Plus OAuth session from ``codex login``.
    api_key_env = "OPENAI_API_KEY"

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,
        session_id: str | None,
        max_turns: int | None,
    ) -> list[str]:
        cmd = [
            self.cli_bin, "exec", prompt,
            "--model", model,
            "--output-format", "json",
        ]
        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])
        if session_id:
            cmd.extend(["--resume", session_id])
        # Codex CLI uses its own default tool set; ``allowed_tools`` is
        # passed through as a best-effort hint when the value is non-empty.
        if allowed_tools:
            cmd.extend(["--tools", allowed_tools])
        return cmd

    def is_rate_limit_error(self, stdout: str, stderr: str, exit_code: int) -> bool:
        if exit_code == 0:
            return False
        return bool(_OPENAI_RATE_LIMIT_RE.search(stdout)) or bool(
            _OPENAI_RATE_LIMIT_RE.search(stderr)
        )

    def parse_output(self, raw_output: str, prior_session_id: str | None) -> AgentResult:
        res = AgentResult(
            raw_output=raw_output,
            session_id=prior_session_id,
        )
        try:
            data = json.loads(raw_output)
            res.result = data.get("result") or data.get("message", {}).get("content", "") or ""
            usage = data.get("usage") or {}
            res.num_turns = int(usage.get("turns", 0) or data.get("num_turns", 0) or 0)
            res.cost_usd = float(usage.get("total_cost_usd") or 0)
            res.session_id = data.get("session_id", prior_session_id) or prior_session_id
        except (json.JSONDecodeError, TypeError, ValueError):
            res.result = raw_output
            logger.warning("OpenAI Codex output was not valid JSON, using raw text")
        return res


# ── Zhipu AI — GLM-5 via the ``glm`` launcher ───────────────────────────────
#
# The ``glm`` binary (github.com/xqsit94/glm) is a thin wrapper around
# Claude Code CLI that redirects the API to Zhipu's Anthropic-compatible
# endpoint at ``open.bigmodel.cn/api/anthropic``. Because it IS Claude
# Code CLI underneath, the command shape, JSON output, tool set, and
# session resume all work identically.
#
# This means GLMBackend inherits from ClaudeBackend and only overrides
# the binary name and the API-key env var. No new parsing logic, no new
# rate-limit regex — everything is Anthropic-shaped.
#
# Install:
#   curl -fsSL https://raw.githubusercontent.com/xqsit94/glm/main/install.sh | bash
#
# API key: register at https://open.bigmodel.cn, go to Console → API Keys.
#   See .env.example for full instructions.
#
# Auth: ``GLM_API_KEY`` env var. The ``glm`` launcher reads it and passes
#   it to the Claude Code CLI as the redirected Anthropic key.


class GLMBackend(ClaudeBackend):
    """Zhipu GLM-5 via the ``glm`` launcher (Claude Code CLI wrapper).

    The ``glm`` binary is Claude Code CLI with the API redirected to
    ``open.bigmodel.cn/api/anthropic``. Same flags, same JSON output,
    same tools, same session resume. Only the binary name and the
    API-key env var differ.

    GLM-5.1 scores 58.4% on SWE-bench Pro (beats Claude Opus 4.6 at
    57.3%) and costs ~$0.95/1M input vs ~$5/1M for Claude — making it
    the best cost/quality tradeoff for overnight batch workloads.

    GLM-5.1 thinking mode
    ---------------------
    Per https://docs.z.ai/guides/overview/migrate-to-glm-new, GLM-5.1
    supports deep thinking via ``thinking={"type": "enabled"}`` in the
    API request body. The ``glm`` launcher passes this through to the
    Zhipu endpoint when ``--model glm-5.1`` is used. For complex
    reasoning and coding tasks, thinking should be enabled (it is
    enabled by default on the Zhipu side for GLM-5.1).
    """

    vendor = "glm"
    label = "GLM (Zhipu)"
    cli_bin = "glm"
    api_key_env = "GLM_API_KEY"


# ── Registry + lookup ───────────────────────────────────────────────────────

_BACKENDS: dict[str, AgentBackend] = {
    "anthropic": ClaudeBackend(),
    "google":    GeminiBackend(),
    "openai":    OpenAIBackend(),
    "glm":       GLMBackend(),
}


def get_backend(vendor: str | None) -> AgentBackend:
    """Return the backend instance for a vendor string.

    Falls back to the Anthropic backend on an unknown or empty vendor so a
    malformed database row never crashes the worker — it just behaves the
    way every pre-AgentBackend task did.
    """
    if vendor and vendor in _BACKENDS:
        return _BACKENDS[vendor]
    if vendor:
        logger.warning("Unknown agent vendor %r, falling back to anthropic", vendor)
    return _BACKENDS[DEFAULT_VENDOR]


def list_vendors() -> list[dict]:
    """Return a UI-ready list of vendor entries with their model lists.

    Shape mirrors what the Next.js combobox endpoint exposes so the worker
    can serve the same data without a separate schema.
    """
    return [
        {
            "id": vendor_id,
            "label": VENDOR_LABELS[vendor_id],
            "models": list(VENDOR_MODELS[vendor_id]),
        }
        for vendor_id in VENDOR_MODELS
    ]
