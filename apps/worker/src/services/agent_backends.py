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
    - :class:`AntigravityBackend` (Google) — ``agy`` CLI, verified on 1.0.10
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
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger(__name__)

#: Windows needs two CLI-spawning workarounds that are unnecessary — and that
#: we deliberately keep *off* — on macOS / Linux: delivering the prompt on
#: stdin (cmd.exe's ~8191-char command-line limit) and resolving the npm
#: ``.CMD`` shim to a full path (``CreateProcess`` ignores ``PATHEXT``). Both
#: are gated on this flag so the POSIX spawn path stays exactly as it was
#: before Windows support landed: prompt passed as an argv element, binary
#: spawned by its bare name via the OS ``PATH`` lookup.
_IS_WINDOWS = sys.platform == "win32"


def _running_as_root() -> bool:
    """True when the worker process runs as uid 0 (the Docker default).

    Used to decide whether the Claude/GLM CLI needs ``IS_SANDBOX=1`` to be
    allowed to run ``--dangerously-skip-permissions`` (which it otherwise
    refuses under root for security reasons).
    """
    try:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        return False


class CLINotInstalledError(RuntimeError):
    """Raised/surfaced when a vendor's CLI binary is not on $PATH."""


# ── Registry: supported vendors and their recommended models ────────────────
#
# These are the choices the task-form combobox presents. Users are still
# allowed to type a free-form model string (repo default, old model, etc.),
# so the list is a hint — not an enforced allow-list.
#
# Ordering matters: the first entry in each list is the "strong" default
# that gets auto-selected when a user switches vendor in the UI.

VENDOR_MODELS: dict[str, list[dict[str, str]]] = {
    # Verified against ``GET https://api.anthropic.com/v1/models`` (2026-07-25).
    # ``claude-opus-5`` is the current flagship Opus (1M ctx, 128K output) at
    # Opus 4.8 pricing; ``claude-mythos-5`` is deliberately absent — it is only
    # served to Project Glasswing accounts.
    "anthropic": [
        {"id": "claude-sonnet-5",              "label": "Claude Sonnet 5 (default, Max)"},
        {"id": "claude-opus-5",                "label": "Claude Opus 5 (flagship — agentic coding, long-horizon)"},
        {"id": "claude-fable-5",               "label": "Claude Fable 5 (most capable, premium)"},
        {"id": "claude-opus-4-8",              "label": "Claude Opus 4.8"},
        {"id": "claude-opus-4-7",              "label": "Claude Opus 4.7"},
        {"id": "claude-opus-4-6",              "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6",            "label": "Claude Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001",    "label": "Claude Haiku 4.5"},
        {"id": "claude-opus-4-5",              "label": "Claude Opus 4.5"},
        {"id": "claude-sonnet-4-5",            "label": "Claude Sonnet 4.5"},
    ],
    # Antigravity CLI (``agy``) model slugs. Since agy 1.1.2 the ``--model``
    # flag is validated strictly and the reasoning-effort level is part of the
    # slug (``<model>-<low|medium|high>``); the old bare slugs
    # (``gemini-3.5-pro`` / ``gemini-3.1-pro`` / ``gemini-3.5-flash``) are
    # rejected with "model … is not recognized". ``gemini-3.5-pro`` no longer
    # exists; Gemini 3.1 Pro is still the strongest coding tier agy offers.
    # All entries below are listed by ``agy models`` **and** smoke-tested with
    # a real ``agy -p`` run on agy 1.1.7 with a Google AI subscription — that
    # includes the ``gemini-3.5-flash-medium`` / ``-high`` pair, which agy
    # 1.1.2 used to reject at run time and now accepts.
    "google": [
        {"id": "gemini-3.1-pro-high",          "label": "Gemini 3.1 Pro (High) — strong coding, default"},
        {"id": "gemini-3.1-pro-low",           "label": "Gemini 3.1 Pro (Low) — faster"},
        {"id": "gemini-3.6-flash-high",        "label": "Gemini 3.6 Flash (High) — newest flash, deeper reasoning"},
        {"id": "gemini-3.6-flash-medium",      "label": "Gemini 3.6 Flash (Medium) — newest flash, balanced"},
        {"id": "gemini-3.6-flash-low",         "label": "Gemini 3.6 Flash (Low) — newest flash, fastest"},
        {"id": "gemini-3.5-flash-low",         "label": "Gemini 3.5 Flash (Low) — fast, cheap"},
    ],
    # Slugs + descriptions taken from the model catalog bundled inside the
    # Codex CLI binary (codex-cli 0.144.1). The GPT-5.6 trio (Sol / Terra /
    # Luna) are the current agentic-coding line. ``gpt-5.5-codex`` used to sit
    # at the top of this list but is *not* in the catalog and is not a real
    # slug — replaced by ``gpt-5.5``.
    "openai": [
        {"id": "gpt-5.6-sol",                  "label": "GPT-5.6 Sol — latest frontier agentic coding"},
        {"id": "gpt-5.6-terra",                "label": "GPT-5.6 Terra — balanced agentic coding"},
        {"id": "gpt-5.6-luna",                 "label": "GPT-5.6 Luna — fast + affordable agentic coding"},
        {"id": "gpt-5.5",                      "label": "GPT-5.5 (frontier: complex coding + research)"},
        {"id": "gpt-5.4",                      "label": "GPT-5.4 (strong everyday coding)"},
        {"id": "gpt-5.3-codex",                "label": "GPT-5.3 Codex (heavy reasoning, agentic)"},
        {"id": "gpt-5.4-mini",                 "label": "GPT-5.4 Mini (small, fast, cost-efficient)"},
        {"id": "gpt-5.2",                      "label": "GPT-5.2 (long-running agents)"},
    ],
    # Verified against ``GET https://open.bigmodel.cn/api/paas/v4/models``
    # (2026-07-25) — Zhipu currently serves glm-4.5, glm-4.5-air, glm-4.6,
    # glm-4.7, glm-5, glm-5-turbo, glm-5.1 and glm-5.2.
    "glm": [
        {"id": "glm-5.2",                      "label": "GLM-5.2 (thinking, latest flagship)"},
        {"id": "glm-5.1",                      "label": "GLM-5.1 (thinking, SWE-bench Pro leader, 8x cheaper)"},
        {"id": "glm-5-turbo",                  "label": "GLM-5 Turbo (fast, cheap)"},
        {"id": "glm-5",                        "label": "GLM-5"},
        {"id": "glm-4.7",                      "label": "GLM-4.7 (previous generation)"},
        {"id": "glm-4.5-air",                  "label": "GLM-4.5 Air (budget)"},
    ],
}

VENDOR_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "google":    "Google (Antigravity)",
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
    #: Default CLI binary name on $PATH.
    cli_bin: str = ""
    #: Name of the ``config.settings`` attribute that overrides ``cli_bin``
    #: per deployment (e.g. ``"claude_bin"`` ← ``CLAUDE_BIN`` env). May be an
    #: absolute path to the binary — required on hosts where the CLI is not on
    #: the worker process's PATH. Empty → no override, always use ``cli_bin``.
    bin_setting: str = ""
    #: One-line shell command that installs this CLI. Shown to the operator
    #: when the binary is missing so the fix is copy-pasteable.
    install_hint: str = ""
    #: When True, this CLI *can* read its prompt from stdin in print mode, so
    #: on Windows we deliver it that way to dodge cmd.exe's ~8191-char
    #: command-line limit (a repo-map+memory prompt blows it via the npm
    #: ``.CMD`` shim → "The command line is too long." → exit 1). On
    #: macOS / Linux the prompt is always passed as an argv element instead —
    #: see ``_deliver_prompt_on_stdin`` — preserving the original behaviour.
    prompt_on_stdin: bool = False

    def _deliver_prompt_on_stdin(self) -> bool:
        """True only when the prompt must go on stdin (Windows + capable CLI).

        ``build_command`` consults this to decide whether to embed the prompt
        as an argv element, and the spawn site calls :meth:`stdin_payload`
        which agrees with it. POSIX → always False → argv delivery.
        """
        return self.prompt_on_stdin and _IS_WINDOWS

    def stdin_payload(self, prompt: str) -> bytes | None:
        """Bytes to feed the CLI on stdin, or ``None`` to pass via argv."""
        if self._deliver_prompt_on_stdin():
            return prompt.encode("utf-8")
        return None

    # ── Availability ────────────────────────────────────────────────────
    def _configured_bin(self) -> str:
        """The CLI name or path: the settings override if set, else ``cli_bin``."""
        if self.bin_setting:
            override = (getattr(settings, self.bin_setting, "") or "").strip()
            if override:
                return override
        return self.cli_bin

    def resolve_bin(self) -> str | None:
        """Resolve the CLI to a runnable path, or ``None`` if not found.

        On Windows an npm-installed CLI is a ``.CMD`` shim. ``CreateProcess``
        (what subprocess uses with ``shell=False``) does not apply ``PATHEXT``,
        so spawning the bare name ``claude`` raises ``FileNotFoundError`` even
        when ``claude.CMD`` is on PATH. We therefore resolve the *full* path
        (including the ``.CMD`` extension via ``shutil.which``) and spawn that.
        An explicit override that is already a path is used verbatim.
        """
        name = self._configured_bin()
        if os.path.isfile(name):
            return name
        return shutil.which(name)

    def bin_argv0(self) -> str:
        """argv[0] for spawning the CLI.

        On macOS / Linux this is the bare configured name (``"claude"``), spawned
        through the OS ``PATH`` lookup exactly as before Windows support landed.
        On Windows we must spawn the *full* resolved path: ``CreateProcess``
        does not apply ``PATHEXT``, so the bare name ``claude`` raises
        ``FileNotFoundError`` even when ``claude.CMD`` is on ``PATH``. Falls
        back to the configured name so a later spawn failure still names it.
        """
        if _IS_WINDOWS:
            return self.resolve_bin() or self._configured_bin()
        return self._configured_bin()

    def is_available(self) -> bool:
        """True if this vendor's CLI binary can be resolved to a runnable path."""
        return self.resolve_bin() is not None

    def not_installed_message(self) -> str:
        """Human-readable error explaining the CLI is missing + how to fix it."""
        name = self._configured_bin()
        msg = (
            f"{self.label} CLI not found: '{name}' is not installed or not on "
            f"the worker's PATH. Install it, or set {self.bin_setting or 'the CLI path'}"
            f" to an absolute path in .env, before running {self.label} tasks."
        )
        if self.install_hint:
            msg += f" Install with: {self.install_hint}"
        return msg

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
    bin_setting = "claude_bin"
    api_key_env = "ANTHROPIC_API_KEY"
    install_hint = "npm install -g @anthropic-ai/claude-code"
    # The CLI can read its prompt from stdin in ``-p`` print mode. We only use
    # that on Windows (cmd.exe command-line limit); POSIX passes it via argv.
    # See AgentBackend.prompt_on_stdin / _deliver_prompt_on_stdin.
    prompt_on_stdin = True

    def build_env(self, billing_mode: str = "api") -> dict[str, str] | None:
        """Claude/GLM env, with the root-sandbox escape hatch.

        Claude Code CLI refuses ``--dangerously-skip-permissions`` when it
        detects it is running as root/sudo — which is exactly how the worker
        runs inside the Docker image. Setting ``IS_SANDBOX=1`` tells the CLI
        the environment is already isolated and lets the flag through. This
        is safe here: DevServer always runs the agent inside a locked,
        throwaway git worktree. Without it, every task (and every system-LLM
        subscription call) fails in Docker with
        "--dangerously-skip-permissions cannot be used with root/sudo
        privileges for security reasons".

        Only materialised when actually running as root, so non-root hosts
        (the typical local dev box) keep inheriting the parent environment
        unchanged.
        """
        env = super().build_env(billing_mode)
        if _running_as_root():
            if env is None:
                env = dict(os.environ)
            env["IS_SANDBOX"] = "1"
        return env

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,
        session_id: str | None,
        max_turns: int | None,
    ) -> list[str]:
        # POSIX (macOS/Linux): prompt is the positional after ``-p`` — the
        # original, proven delivery. Windows: prompt goes on stdin (see
        # _deliver_prompt_on_stdin) to avoid cmd.exe's command-line limit, so
        # ``-p`` is given with no positional and the spawn site supplies stdin.
        cmd = [self.bin_argv0(), "-p"]
        if not self._deliver_prompt_on_stdin():
            cmd.append(prompt)
        cmd += [
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
            snippet = (raw_output or "").strip()[:500] or "<empty>"
            logger.warning(
                "Claude output was not valid JSON, using raw text. First 500 chars: %s",
                snippet,
            )
        return res


# ── Google — Antigravity CLI (``agy``) ──────────────────────────────────────
#
# Google retired the Gemini CLI on 2026-06-18 (auth endpoint returns HTTP
# 410 Gone) and replaced it with the **Antigravity CLI**, a closed-source Go
# binary invoked as ``agy``. It is NOT a drop-in rename — different binary,
# different flags, plain-text (not JSON) print output. Verified against
# ``agy`` 1.0.10 on a real machine; the command shape below is what the
# installed ``--help`` actually exposes, not what third-party blogs claim.
#
# Command shape (headless / non-interactive):
#     agy -p <prompt> --model <slug> --dangerously-skip-permissions
#         --print-timeout <dur>
#
#   -p / --print                    single-prompt headless mode (fully
#                                   agentic — it edits files & runs tools,
#                                   verified by creating files in a temp repo)
#   --model <slug>                  e.g. ``gemini-3.1-pro-high`` /
#                                   ``gemini-3.5-flash-low`` (agy 1.1.2 bakes
#                                   the reasoning effort into the slug and
#                                   validates it strictly)
#   --dangerously-skip-permissions  auto-approve every tool call (required
#                                   for an unattended worker; the only
#                                   auto-approve flag ``agy`` has)
#   --print-timeout <go-duration>   internal wait cap; default 5m would cut
#                                   off long agent runs, so we raise it well
#                                   above the worker's own subprocess timeout
#
# NOT available in ``agy`` 1.0.10 (intentionally dropped vs. the old Gemini
# backend): ``--output-format`` (print mode emits plain text), ``--max-turns``
# (no turn cap flag), ``--skip-trust`` (no trust gate), ``-y/--yolo``. Session
# resume exists only as ``--continue`` / ``--conversation <ID>``, but print
# mode emits no conversation ID to capture, so each call runs fresh (like the
# Codex backend). An unknown ``--model`` is *silently ignored* (falls back to
# the default and exits 0) — there is no model-validation error to detect.
#
# Auth: per the user's requirement, this backend keeps the *same* auth as the
# old Gemini backend — the Google AI Pro/Ultra **subscription** (browser
# OAuth, used in ``max`` mode) and the **same key**, ``GEMINI_API_KEY`` (used
# in ``api`` mode). ``agy``'s own documented key var is ``ANTIGRAVITY_API_KEY``,
# so in ``api`` mode we mirror ``GEMINI_API_KEY`` into it when it isn't
# already set, and in ``max`` mode strip both so the OAuth subscription wins.
#
# Rate-limit detection: Google surfaces 429s as a literal "429" or a
# ``RESOURCE_EXHAUSTED`` / quota-exceeded line.

_GEMINI_RATE_LIMIT_RE = re.compile(
    r"RESOURCE_EXHAUSTED|429|quota.*exceeded|rate.?limit",
    re.IGNORECASE,
)


class AntigravityBackend(AgentBackend):
    """Google Antigravity CLI (``agy``) backend — the Gemini CLI successor.

    Verified end-to-end against ``agy`` 1.0.10: headless ``-p`` runs are
    fully agentic (edit files, run tools) and print plain text. Keeps the
    Gemini backend's auth model: Google AI Pro/Ultra subscription via OAuth
    (``max``) and ``GEMINI_API_KEY`` (``api``).
    """

    vendor = "google"
    label = "Google (Antigravity)"
    cli_bin = "agy"
    bin_setting = "gemini_bin"
    install_hint = "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    # Per the operator's requirement we reuse the *old Gemini* key here so no
    # new secret is needed. ``agy`` itself reads ``ANTIGRAVITY_API_KEY``; we
    # bridge the two in :meth:`build_env`.
    api_key_env = "GEMINI_API_KEY"

    #: ``agy``'s own documented API-key env var. In ``api`` mode we populate
    #: it from ``GEMINI_API_KEY`` so the existing key keeps working.
    _AGY_API_KEY_ENV = "ANTIGRAVITY_API_KEY"

    #: Remap legacy bare slugs (accepted by the retired Gemini CLI and by
    #: agy < 1.1.0) onto the effort-suffixed slugs agy 1.1.2 requires, so
    #: tasks created before this change keep running instead of failing with
    #: "model … is not recognized". ``gemini-3.5-pro`` no longer exists at
    #: Google, so it falls back to the strongest available Gemini model.
    _LEGACY_MODEL_ALIASES = {
        "gemini-3.5-pro":   "gemini-3.1-pro-high",
        "gemini-3.1-pro":   "gemini-3.1-pro-high",
        "gemini-3.5-flash": "gemini-3.5-flash-low",
    }

    #: Internal print-mode wait cap handed to ``--print-timeout``. ``agy``
    #: defaults to 5m, which would abort long agent runs; set it well above
    #: any worker subprocess timeout so the real bound stays in agent_runner.
    _PRINT_TIMEOUT = "180m"

    #: Every env var that would override the OAuth "Login with Google"
    #: subscription path. In ``max`` (subscription) mode all of these must be
    #: absent or the CLI prefers key / Vertex auth and the user's Google AI
    #: Pro / Ultra subscription is never used.
    _SUBSCRIPTION_BLOCKING_ENV = (
        "ANTIGRAVITY_API_KEY",        # agy's native API key
        "GEMINI_API_KEY",             # AI Studio API key (bridged in api mode)
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",  # forces Vertex AI auth
        "GOOGLE_CLOUD_PROJECT",       # Vertex / ADC project selector
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",  # ADC service-account file
    )

    def build_env(self, billing_mode: str = "api") -> dict[str, str] | None:
        """Subscription-aware env for ``agy``.

        ``'max'`` means "use my Google AI Pro / Ultra subscription" — the
        browser-OAuth login ``agy`` stores on first run. That path is only
        chosen when *none* of the key / Vertex env vars are set, so we strip
        the whole :data:`_SUBSCRIPTION_BLOCKING_ENV` set. ``'api'`` keeps the
        full environment and bridges ``GEMINI_API_KEY`` → ``ANTIGRAVITY_API_KEY``
        (the var ``agy`` actually reads) when the latter isn't already set.
        """
        if billing_mode == "max":
            return {
                k: v
                for k, v in os.environ.items()
                if k not in self._SUBSCRIPTION_BLOCKING_ENV
            }
        # api mode: reuse the old Gemini key with the new CLI.
        key = os.environ.get(self.api_key_env)
        if key and not os.environ.get(self._AGY_API_KEY_ENV):
            env = dict(os.environ)
            env[self._AGY_API_KEY_ENV] = key
            return env
        return None

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,  # noqa: ARG002 — agy has no tool-allow-list flag
        session_id: str | None,  # noqa: ARG002 — print mode has no resumable ID
        max_turns: int | None,  # noqa: ARG002 — agy has no --max-turns flag
    ) -> list[str]:
        # AUTONOMOUS / HEADLESS — these flags are mandatory. Do not remove:
        #   -p <prompt>                     non-interactive print mode (else a TUI).
        #   --dangerously-skip-permissions  auto-approve every tool call; without
        #                                   it the agent deadlocks waiting for a
        #                                   confirmation no headless worker can give.
        #   --print-timeout                 raised above the default 5m so long
        #                                   agent runs aren't cut off internally.
        #
        # session_id: ``agy`` print mode emits no conversation ID to capture, so
        #   each invocation runs fresh (resume is interactive-only). Same as Codex.
        # max_turns / allowed_tools: ``agy`` has no equivalent flags — ignored.
        cmd = [
            self.bin_argv0(), "-p", prompt,
            "--model", self._LEGACY_MODEL_ALIASES.get(model, model),
            "--dangerously-skip-permissions",
            "--print-timeout", self._PRINT_TIMEOUT,
        ]
        return cmd

    def is_rate_limit_error(self, stdout: str, stderr: str, exit_code: int) -> bool:
        if exit_code == 0:
            return False
        return bool(_GEMINI_RATE_LIMIT_RE.search(stdout)) or bool(
            _GEMINI_RATE_LIMIT_RE.search(stderr)
        )

    def parse_output(self, raw_output: str, prior_session_id: str | None) -> AgentResult:
        """``agy`` print mode emits the assistant's reply as plain text.

        There is no JSON envelope (no ``--output-format`` in 1.0.10), so the
        whole stdout *is* the result. Cost is 0 (subscription/OAuth bills
        flat) and turn count is unavailable.
        """
        return AgentResult(
            raw_output=raw_output,
            result=(raw_output or "").strip(),
            session_id=prior_session_id,
        )


# ── OpenAI — Codex CLI ──────────────────────────────────────────────────────
#
# Command shape (openai/codex ≥ 0.120, Rust rewrite):
#     codex exec [OPTIONS] [PROMPT]
#
# Supported flags (what we use):
#   --json                         JSONL event stream on stdout
#   --model <MODEL>                model name (or Azure deployment name)
#   --full-auto                    workspace-write sandbox, no prompts
#   --skip-git-repo-check          let codex run in existing git worktree
#   -C, --cd <DIR>                 working root
#   -c key=value                   TOML config override (repeatable)
#
# NOT supported by current codex (intentionally dropped):
#   --output-format, --max-turns, --resume, --tools
#   Session resume is a distinct subcommand (``codex exec resume``) with a
#   different shape — skipped here; each invocation runs fresh.
#
# Auth: reads ``OPENAI_API_KEY`` from env, or uses the ChatGPT OAuth login
# stored by ``codex login``. For Azure AI Foundry, OPENAI_API_KEY is the
# Azure resource key and extra ``-c`` overrides route codex through the
# Azure provider (see build_command below).
#
# Rate-limit detection: OpenAI returns ``rate_limit_exceeded`` in the error
# JSON or a literal 429.

_OPENAI_RATE_LIMIT_RE = re.compile(
    r"rate_limit_exceeded|429|RateLimitError",
    re.IGNORECASE,
)


class OpenAIBackend(AgentBackend):
    """OpenAI Codex CLI backend (codex-cli ≥ 0.120)."""

    vendor = "openai"
    label = "OpenAI"
    cli_bin = "codex"
    bin_setting = "codex_bin"
    install_hint = "npm install -g @openai/codex"
    # Codex CLI reads OPENAI_API_KEY. In 'max' mode we strip it so the
    # CLI falls back to the ChatGPT-Plus OAuth session from ``codex login``.
    api_key_env = "OPENAI_API_KEY"

    #: ``gpt-5.5-codex`` shipped as this backend's default for a while but is
    #: not in Codex's model catalog and is rejected by the CLI. Tasks and
    #: templates created back then still carry it, so remap onto the real
    #: slug instead of failing the run. Skipped when OPENAI_BASE_URL points at
    #: Azure, where the "model" is a user-chosen deployment name.
    _LEGACY_MODEL_ALIASES = {
        "gpt-5.5-codex": "gpt-5.5",
    }

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,  # noqa: ARG002 — codex has no equivalent flag
        session_id: str | None,  # noqa: ARG002 — see module docstring
        max_turns: int | None,  # noqa: ARG002 — codex has no --max-turns
    ) -> list[str]:
        # Azure AI Foundry routing. When OPENAI_BASE_URL is set we assume
        # the user wants an OpenAI-compatible alternative endpoint (Azure
        # is the only supported case today). Rather than ask the user to
        # maintain ``~/.codex/config.toml``, we synthesise an ``azure``
        # provider via repeatable ``-c key=value`` TOML overrides. This is
        # the same shape documented at
        # https://github.com/openai/codex (Azure provider section).
        try:
            from config import settings  # local import to avoid cycles at module load
        except Exception:
            settings = None  # type: ignore[assignment]

        base_url = getattr(settings, "openai_base_url", "") if settings else ""
        api_version = getattr(settings, "openai_api_version", "") if settings else ""

        cmd: list[str] = [
            self.bin_argv0(), "exec",
            "--json",                      # JSONL event stream on stdout
            "--skip-git-repo-check",       # worktrees are valid repos but already on a branch
            "--full-auto",                 # workspace-write sandbox + no approval prompts
        ]
        if model:
            resolved = model if base_url else self._LEGACY_MODEL_ALIASES.get(model, model)
            cmd.extend(["--model", resolved])

        if base_url:
            # Azure wants the ``/openai`` suffix appended when it's not
            # already present; accept either form from the user.
            endpoint = base_url.rstrip("/")
            if not endpoint.endswith("/openai"):
                endpoint = f"{endpoint}/openai"
            cmd.extend([
                "-c", 'model_provider="azure"',
                "-c", 'model_providers.azure.name="Azure OpenAI"',
                "-c", f'model_providers.azure.base_url="{endpoint}"',
                "-c", 'model_providers.azure.env_key="OPENAI_API_KEY"',
                "-c", 'model_providers.azure.wire_api="responses"',
            ])
            if api_version:
                cmd.extend([
                    "-c",
                    f'model_providers.azure.query_params={{api-version="{api_version}"}}',
                ])

        # Prompt is positional and must come last.
        cmd.append(prompt)
        return cmd

    def is_rate_limit_error(self, stdout: str, stderr: str, exit_code: int) -> bool:
        if exit_code == 0:
            return False
        return bool(_OPENAI_RATE_LIMIT_RE.search(stdout)) or bool(
            _OPENAI_RATE_LIMIT_RE.search(stderr)
        )

    def parse_output(self, raw_output: str, prior_session_id: str | None) -> AgentResult:
        """Parse codex's JSONL event stream.

        codex 0.120 emits dot-namespaced event types on stdout, one JSON
        object per line. Observed shapes:

            {"type":"thread.started","thread_id":"019d9172-..."}
            {"type":"turn.started"}
            {"type":"agent.message","message":"..."}           (speculative)
            {"type":"turn.completed","usage":{"total_tokens":123}}  (speculative)
            {"type":"error","message":"..."}

        We also tolerate underscore-namespaced variants (older codex) and
        an optional ``msg`` envelope. Cost isn't reported directly — codex
        only gives tokens — so ``cost_usd`` stays 0 and the budget gate
        treats OpenAI runs as free until we wire a tokens→$ table.
        """
        res = AgentResult(
            raw_output=raw_output,
            session_id=prior_session_id,
        )

        last_message = ""
        errors: list[str] = []
        turns = 0
        total_tokens = 0
        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("msg") if isinstance(event.get("msg"), dict) else event
            etype = (payload.get("type") or event.get("type") or "").replace("_", ".")

            if etype in ("thread.started", "task.started"):
                sid = (
                    payload.get("thread_id")
                    or payload.get("session_id")
                    or event.get("thread_id")
                    or event.get("session_id")
                )
                if sid:
                    res.session_id = sid
            elif etype in ("agent.message", "agent_message", "message"):
                msg = payload.get("message") or payload.get("content") or ""
                if isinstance(msg, str) and msg:
                    last_message = msg
                    turns += 1
            elif etype in ("turn.completed", "token_count"):
                usage = payload.get("usage") or {}
                tt = usage.get("total_tokens") or usage.get("total") or 0
                try:
                    total_tokens = int(tt) or total_tokens
                except (TypeError, ValueError):
                    pass
            elif etype in ("task.complete", "task_complete", "thread.completed"):
                final = payload.get("last_agent_message") or payload.get("message")
                if isinstance(final, str) and final:
                    last_message = final
            elif etype == "error":
                err_msg = payload.get("message") or payload.get("error") or ""
                if isinstance(err_msg, str) and err_msg:
                    errors.append(err_msg)

        if last_message:
            res.result = last_message
        elif errors:
            # Surface the first error so the dashboard shows something
            # actionable (e.g. Azure 404s) instead of a silent failure.
            res.result = "\n".join(errors[:3])
            res.error = errors[0]
            logger.warning("OpenAI Codex reported errors: %s", errors[0])
        elif raw_output:
            res.result = raw_output[:2000]
            logger.warning(
                "OpenAI Codex produced no agent.message events; storing raw output",
            )

        res.num_turns = turns
        # Surface token counts + any non-fatal errors in ``errors`` so the
        # dashboard can display them without a new AgentResult field.
        if total_tokens:
            res.errors.append(f"total_tokens={total_tokens}")
        res.errors.extend(errors)
        return res


# ── Zhipu AI — GLM-5 via the Claude Code CLI + Zhipu endpoint ───────────────
#
# DevServer runs GLM by invoking the *real* ``claude`` binary with its API
# redirected to Zhipu's Anthropic-compatible endpoint at
# ``open.bigmodel.cn/api/anthropic`` — exactly what the standalone ``glm``
# launcher (github.com/xqsit94/glm) does internally (set ANTHROPIC_BASE_URL +
# ANTHROPIC_AUTH_TOKEN, then exec ``claude``).
#
# We deliberately do NOT shell out to the ``glm`` launcher: newer ``glm``
# releases are an interactive Claude launcher whose own flags are only
# ``-m/--model`` / ``--yolo`` / subcommands — it does not forward Claude
# CLI args, so ``glm -p ... --output-format json`` fails with
# ``unknown command "json" for "glm"``. Driving ``claude`` directly with the
# GLM endpoint env vars is launcher-version-independent and the right shape
# for programmatic (``-p``) runs.
#
# Because the underlying CLI is Claude Code, the command shape, JSON output,
# tool set, and session resume all work identically — GLMBackend inherits
# everything from ClaudeBackend and only overrides the env wiring.
#
# API key: register at https://open.bigmodel.cn, go to Console → API Keys,
#   set ``GLM_API_KEY`` in ``.env``. See .env.example for full instructions.


class GLMBackend(ClaudeBackend):
    """Zhipu GLM-5 via the Claude Code CLI pointed at Zhipu's endpoint.

    Runs the real ``claude`` binary (not the ``glm`` launcher) with
    ``ANTHROPIC_BASE_URL`` → ``open.bigmodel.cn/api/anthropic`` and
    ``ANTHROPIC_AUTH_TOKEN`` → ``GLM_API_KEY``. Same flags, same JSON
    output, same tools, same session resume — only the env differs.

    GLM-5.1 scores 58.4% on SWE-bench Pro (beats Claude Opus 4.6 at
    57.3%) and costs ~$0.95/1M input vs ~$5/1M for Claude — making it
    the best cost/quality tradeoff for overnight batch workloads.

    Billing mode
    ------------
    GLM has no OAuth/subscription path (the ``glm`` ecosystem *is* the
    key-authenticated Zhipu endpoint), so both ``'api'`` and ``'max'``
    behave the same: the request is always authenticated with
    ``GLM_API_KEY`` via ``ANTHROPIC_AUTH_TOKEN``.

    GLM-5.1 thinking mode
    ---------------------
    Per https://docs.z.ai/guides/overview/migrate-to-glm-new, GLM-5.1
    supports deep thinking via ``thinking={"type": "enabled"}`` in the
    API request body — enabled by default on the Zhipu side for GLM-5.1.
    """

    vendor = "glm"
    label = "GLM (Zhipu)"
    # The real Claude Code CLI; the ``glm`` launcher is not used (see above).
    cli_bin = "claude"
    api_key_env = "GLM_API_KEY"
    install_hint = (
        "npm install -g @anthropic-ai/claude-code and set GLM_API_KEY in .env "
        "(register at https://open.bigmodel.cn)"
    )

    #: Zhipu's Anthropic-compatible endpoint. Overridable via ``GLM_BASE_URL``.
    default_base_url = "https://open.bigmodel.cn/api/anthropic"

    def build_env(self, billing_mode: str = "api") -> dict[str, str] | None:
        """Route the Claude CLI to Zhipu, authenticated with ``GLM_API_KEY``.

        Always uses the key path (GLM has no subscription/OAuth fallback),
        so ``billing_mode`` is ignored beyond inheriting ClaudeBackend's
        root-sandbox (``IS_SANDBOX``) handling.
        """
        # Force 'api' into the parent so it never strips GLM_API_KEY; this
        # also gives us the IS_SANDBOX escape hatch when running as root.
        env = super().build_env("api")
        if env is None:
            env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = os.environ.get(
            "GLM_BASE_URL", self.default_base_url
        )
        token = os.environ.get(self.api_key_env)
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
        # A stray real-Anthropic key would otherwise shadow the GLM token
        # against the redirected endpoint and 401.
        env.pop("ANTHROPIC_API_KEY", None)
        return env


# ── Registry + lookup ───────────────────────────────────────────────────────

_BACKENDS: dict[str, AgentBackend] = {
    "anthropic": ClaudeBackend(),
    "google":    AntigravityBackend(),
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
