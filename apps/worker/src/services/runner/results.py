"""What an attempt can end in, and what a successful one produced.

Kept apart from :mod:`.attempt` so that :mod:`.failures` and :mod:`.outcome`
can speak the same vocabulary without importing the attempt machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .delivery import AttemptMetrics, Handover


class Verdict(StrEnum):
    SUCCEEDED = "succeeded"
    RETRY = "retry"  # try again with the same model
    ESCALATE = "escalate"  # this model is stuck; stop its attempts
    BLOCKED = "blocked"  # PR preflight found a hard violation
    BUDGET_EXCEEDED = "budget_exceeded"
    SUSPENDED = "suspended"  # waiting on a side-effect gate decision


@dataclass(frozen=True)
class Success:
    metrics: AttemptMetrics
    handover: Handover
    result_text: str
    files_changed: tuple[str, ...]


@dataclass(frozen=True)
class AttemptResult:
    verdict: Verdict
    success: Success | None = None
    reason: str = ""
    violations: tuple[dict, ...] = ()
