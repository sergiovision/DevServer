"""How a failed attempt turns into the next step: retry, back off or escalate.

A failure is classified (:mod:`services.error_classifier`) into a targeted
hint for the next attempt's prompt. The same error class twice, or a "hard"
class, means retrying the same model will not help, so the attempt loop is
told to escalate (to the backup model, or to a failed task).
"""

from __future__ import annotations

import asyncio
import logging

from services import error_classifier
from services.agent_backends import AgentBackend, AgentResult
from services.retry import Backoff

from . import store
from .context import TaskContext
from .preflight import PreflightOutcome, PreflightVerdict
from .results import AttemptResult, Verdict

logger = logging.getLogger(__name__)

#: Pause before the next attempt after an ``api.rate_limit`` failure.
ATTEMPT_RATE_LIMIT_BACKOFF = Backoff(delays=(60, 120, 240))
#: The same error class this many times means retrying will not help.
ESCALATE_AFTER_REPEATS = 2


def _is_max_turns(backend: AgentBackend, result: AgentResult) -> bool:
    # Antigravity signals "turn limit reached" with exit code 53 only.
    google_turn_limit = backend.vendor == "google" and result.exit_code == 53
    return google_turn_limit or result.subtype == "error_max_turns"


async def agent_failed(
    ctx: TaskContext,
    backend: AgentBackend,
    result: AgentResult,
    *,
    run_id: int,
    attempt: int,
    duration_ms: int,
) -> AttemptResult:
    if _is_max_turns(backend, result):
        # Progress was made; resume the same session on the next attempt.
        ctx.session_id = result.session_id
        ctx.log.write(
            f"\n[Attempt {attempt}] PAUSED: max_turns reached (turns={result.num_turns})"
            f" — will resume session {result.session_id}\n"
        )
        await store.finish_run(
            run_id,
            status="failed",
            error_log="max_turns reached, resuming",
            duration_ms=duration_ms,
        )
        return AttemptResult(Verdict.RETRY)

    reason = "; ".join(result.errors) if result.errors else f"exit code {result.exit_code}"
    if result.subtype:
        reason = f"{result.subtype}: {reason}"
    logger.error("Agent failed on attempt %d/%d: %s", attempt, ctx.repo.max_retries, reason)
    ctx.log.write(f"\n[Attempt {attempt}] FAILED: {reason}\n")

    error_class = await _classify(ctx, result.raw_output, run_id=run_id, source=None)
    await store.finish_run(run_id, status="failed", error_log=reason[:500], duration_ms=duration_ms)

    if error_class is not None and error_class.key == "api.rate_limit":
        return await _back_off_after_rate_limit(ctx, run_id=run_id)
    return _retry_or_escalate(ctx, error_class)


async def _back_off_after_rate_limit(ctx: TaskContext, *, run_id: int) -> AttemptResult:
    hits = ctx.error_class_counts["api.rate_limit"]
    if hits > len(ATTEMPT_RATE_LIMIT_BACKOFF.delays):
        logger.warning("Rate limit: exhausted %d backoff retries, escalating", hits - 1)
        return AttemptResult(Verdict.ESCALATE)
    wait_seconds = ATTEMPT_RATE_LIMIT_BACKOFF.delay(hits - 1)
    await store.emit_event(
        ctx.task.id,
        run_id,
        "log_line",
        {"line": f"[rate_limit] backoff {wait_seconds:.0f}s (hit #{hits})", "stream": "stderr"},
    )
    ctx.log.note("rate_limit", f"sleeping {wait_seconds:.0f}s before retry")
    await asyncio.sleep(wait_seconds)
    return AttemptResult(Verdict.RETRY)


async def _classify(
    ctx: TaskContext, output: str, *, run_id: int, source: str | None
) -> error_classifier.ErrorClass | None:
    """Turn a failure into a targeted hint for the next attempt's prompt."""
    error_class = error_classifier.classify(output)
    ctx.error_context = error_classifier.build_remediation_block(error_class, output)
    if error_class is None:
        return None
    ctx.error_class_counts[error_class.key] += 1
    payload = {
        "class": error_class.key,
        "severity": error_class.severity,
        "hint": error_class.hint,
        "repeat": ctx.error_class_counts[error_class.key],
    }
    if source:
        payload["source"] = source
    await store.emit_event(ctx.task.id, run_id, "error_classified", payload)
    return error_class


def _retry_or_escalate(
    ctx: TaskContext, error_class: error_classifier.ErrorClass | None
) -> AttemptResult:
    if error_class is None:
        return AttemptResult(Verdict.RETRY)
    repeats = ctx.error_class_counts[error_class.key]
    if repeats >= ESCALATE_AFTER_REPEATS or error_class.severity == "hard":
        logger.warning(
            "Escalating: error class %s repeated %d times (severity=%s)",
            error_class.key,
            repeats,
            error_class.severity,
        )
        return AttemptResult(Verdict.ESCALATE)
    return AttemptResult(Verdict.RETRY)


async def verify_failed(
    ctx: TaskContext, error: str, *, run_id: int, attempt: int
) -> AttemptResult:
    logger.error("Verification failed on attempt %d", attempt)
    error_class = await _classify(ctx, error, run_id=run_id, source="verifier")
    await store.finish_run(
        run_id, status="failed", error_log=f"Verification failed: {error[:5000]}"
    )
    await store.set_task_status(ctx.task.id, "running")
    await store.emit_event(
        ctx.task.id, run_id, "error", {"error": error[:2000], "attempt": attempt}
    )
    return _retry_or_escalate(ctx, error_class)


async def preflight_failed(
    ctx: TaskContext, preflight: PreflightOutcome, *, run_id: int
) -> AttemptResult:
    if preflight.verdict is PreflightVerdict.HARD_FAILURE:
        logger.error("PR preflight hard failure on %s: %s", ctx.task.key, preflight.violations_text)
        await store.finish_run(
            run_id,
            status="failed",
            error_log=f"pr_preflight hard failure: {preflight.violations_text[:500]}",
        )
        return AttemptResult(
            Verdict.BLOCKED, reason=preflight.violations_text, violations=preflight.violations
        )

    # Scope creep only: recoverable, with a targeted hint for the retry.
    ctx.error_context = preflight.hint
    await store.finish_run(
        run_id,
        status="failed",
        error_log=f"pr_preflight scope creep: {preflight.violations_text[:500]}",
    )
    await store.set_task_status(ctx.task.id, "running")
    return AttemptResult(Verdict.RETRY)
