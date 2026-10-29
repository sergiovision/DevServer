"""Deterministic PR preflight, run between a passing verify and the handover.

Wraps :mod:`services.pr_preflight` with the runner's bookkeeping: the
pass/fail event, the Task Log line, and a three-way verdict the attempt can
act on. Hard violations (a leaked secret, an oversize file, a wrong author)
block the task; scope creep outside an approved plan is retried with a hint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services import pr_preflight

from . import store
from .context import TaskContext


class PreflightVerdict(StrEnum):
    PASSED = "passed"
    SCOPE_CREEP = "scope_creep"
    HARD_FAILURE = "hard_failure"


@dataclass(frozen=True)
class PreflightOutcome:
    verdict: PreflightVerdict
    files_changed: tuple[str, ...]
    violations: tuple[dict, ...] = ()
    violations_text: str = ""
    hint: str = ""


async def run_preflight(ctx: TaskContext, *, run_id: int) -> PreflightOutcome:
    result = await pr_preflight.run_preflight(
        worktree_path=ctx.worktree_path,
        base_branch=ctx.repo.default_branch,
        allowlist=ctx.preflight_allowlist,
        # Local repos commit with the operator's own identity and never push,
        # so the author check means nothing there.
        skip_author_check=ctx.repo.is_local,
    )
    summary = pr_preflight.summarise(result)
    files_changed = tuple(result.files_changed)

    if result.ok:
        await store.emit_event(ctx.task.id, run_id, "pr_preflight_pass", summary)
        ctx.log.note("pr_preflight", f"PASS — {summary.get('files_changed', 0)} files")
        return PreflightOutcome(PreflightVerdict.PASSED, files_changed)

    await store.emit_event(ctx.task.id, run_id, "pr_preflight_fail", summary)
    violations_text = ", ".join(f"{v.kind}:{v.path or ''}" for v in result.violations[:8])
    ctx.log.note("pr_preflight", f"FAIL — {violations_text}")
    verdict = (
        PreflightVerdict.HARD_FAILURE if result.has_hard_failure else PreflightVerdict.SCOPE_CREEP
    )
    return PreflightOutcome(
        verdict,
        files_changed,
        violations=tuple(
            {"kind": v.kind, "detail": v.detail, "severity": v.severity} for v in result.violations
        ),
        violations_text=violations_text,
        hint=result.hint,
    )
