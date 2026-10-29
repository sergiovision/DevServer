"""One invocation of a vendor's coding-agent CLI.

Vendor specifics (argv, environment, output format, what a 429 looks like)
live in :mod:`services.agent_backends`. This module holds the shared
machinery: install-on-first-use, spawning with a timeout, retrying the same
call when the vendor rate-limits it, and surfacing stderr as task events.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from config import settings
from services import agent_cli_installer
from services import proc as proc_util
from services.agent_backends import AgentBackend, AgentResult
from services.retry import Backoff, RetryNotice, retry_async

from . import store

logger = logging.getLogger(__name__)

#: A vendor 429 burns no agent progress, so the same call is retried after a
#: pause instead of spending one of the task's attempts on it.
RATE_LIMIT_BACKOFF = Backoff(delays=(30, 60, 120), jitter=10)


def _failed(backend_message: str, session_id: str | None) -> AgentResult:
    return AgentResult(exit_code=-1, session_id=session_id, error=backend_message)


def _subprocess_env(backend: AgentBackend, billing_mode: str) -> dict[str, str] | None:
    """The backend's env, plus the Azure/OpenAI-compatible endpoint overrides.

    pydantic-settings reads these from ``.env`` but does not export them, so
    they are forwarded explicitly for the Codex CLI.
    """
    env = backend.build_env(billing_mode=billing_mode)
    if backend.vendor != "openai" or not (settings.openai_base_url or settings.openai_api_version):
        return env
    env = dict(env if env is not None else os.environ)
    if settings.openai_base_url:
        env["OPENAI_BASE_URL"] = settings.openai_base_url
        env.setdefault("AZURE_OPENAI_ENDPOINT", settings.openai_base_url)
    if settings.openai_api_version:
        env["OPENAI_API_VERSION"] = settings.openai_api_version
        env.setdefault("AZURE_OPENAI_API_VERSION", settings.openai_api_version)
    if settings.openai_api_key:
        env.setdefault("AZURE_OPENAI_API_KEY", settings.openai_api_key)
    return env


@dataclass(frozen=True)
class AgentInvocation:
    """Everything needed to run the agent CLI once."""

    backend: AgentBackend
    worktree_path: str
    prompt: str
    model: str
    allowed_tools: str
    session_id: str | None
    timeout_minutes: int
    task_id: int
    run_id: int | None
    billing_mode: str
    max_turns: int | None

    async def emit(self, event_type: str, payload: dict) -> None:
        await store.emit_event(self.task_id, self.run_id, event_type, payload)


async def run_agent(call: AgentInvocation) -> AgentResult:
    """Run the agent CLI once (plus rate-limit retries) and parse its output.

    Never raises for an agent-side failure: a missing CLI, a timeout or a
    non-zero exit all come back as an :class:`AgentResult` with a non-zero
    ``exit_code``. Cancellation propagates (and kills the CLI).
    """
    backend = call.backend
    if not await agent_cli_installer.ensure_cli(backend, on_event=call.emit):
        message = backend.not_installed_message()
        logger.error(message)
        await call.emit("log_line", {"line": message, "stream": "stderr"})
        return _failed(message, call.session_id)

    logger.info(
        "Running %s CLI in %s (model=%s, timeout=%dm, billing=%s)",
        backend.label,
        call.worktree_path,
        call.model,
        call.timeout_minutes,
        call.billing_mode,
    )
    try:
        exit_code, stdout, stderr = await retry_async(
            lambda: _spawn(call),
            attempts=len(RATE_LIMIT_BACKOFF.delays) + 1,
            backoff=RATE_LIMIT_BACKOFF,
            retry_on=lambda out: backend.is_rate_limit_error(out[1], out[2], out[0]),
            on_retry=lambda notice: _announce_backoff(call, notice),
        )
    except TimeoutError:
        message = f"{backend.label} CLI timed out after {call.timeout_minutes}m"
        return _failed(message, call.session_id)
    except FileNotFoundError:
        # The binary vanished between the availability check and the spawn.
        message = backend.not_installed_message()
        logger.error(message)
        return _failed(message, call.session_id)

    for line in stderr.splitlines():
        if line.strip():
            await call.emit("log_line", {"line": line, "stream": "stderr"})
    result = backend.parse_output(stdout, call.session_id)
    result.exit_code = exit_code
    return result


async def _spawn(call: AgentInvocation) -> tuple[int, str, str]:
    """One subprocess run; returns ``(exit_code, stdout, stderr)``."""
    cmd = call.backend.build_command(
        prompt=call.prompt,
        model=call.model,
        allowed_tools=call.allowed_tools,
        session_id=call.session_id,
        max_turns=call.max_turns,
    )
    stdin_payload = call.backend.stdin_payload(call.prompt)
    # stdin=DEVNULL unless the prompt goes on stdin: an inherited TTY can
    # block the CLI or leak stray bytes into the prompt.
    exit_code, stdout, stderr = await proc_util.run(
        cmd,
        cwd=call.worktree_path,
        env=_subprocess_env(call.backend, call.billing_mode),
        timeout=call.timeout_minutes * 60,
        stdin_devnull=stdin_payload is None,
        stdin_input=stdin_payload,
    )
    return exit_code, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _announce_backoff(call: AgentInvocation, notice: RetryNotice) -> None:
    logger.warning(
        "%s 429 (attempt %d/%d) — sleeping %.0fs before retrying",
        call.backend.label,
        notice.retry_number,
        notice.max_retries,
        notice.sleep_seconds,
    )
    await call.emit(
        "rate_limit_backoff",
        {
            "vendor": call.backend.vendor,
            "attempt": notice.retry_number,
            "max_attempts": notice.max_retries,
            "sleep_seconds": round(notice.sleep_seconds, 1),
        },
    )
