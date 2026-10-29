"""Per-task budget circuit breaker.

A task may cap its total agent spend (``tasks.max_cost_usd``) and its total
agent-active wall time (``tasks.max_wall_seconds``). The runner checks the
caps before every attempt so a runaway retry loop stops instead of quietly
burning money. The check is a pure function of what was spent so far.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

#: Fraction of a cap at which the operator gets a one-off warning.
WARN_FRACTION = Decimal("0.8")


class BudgetState(StrEnum):
    OK = "ok"
    WARN = "warn"
    EXCEEDED = "exceeded"


@dataclass(frozen=True)
class BudgetVerdict:
    state: BudgetState
    reason: str = ""


@dataclass(frozen=True)
class BudgetLimits:
    """The caps of one task. ``None`` means no cap on that dimension."""

    max_cost_usd: Decimal | None
    max_wall_seconds: int | None
    #: Subscription billing ("max") reports no real cost, so cost caps are
    #: skipped there.
    billing_mode: str


def check_budget(*, cost_usd: Decimal, wall_ms: int, limits: BudgetLimits) -> BudgetVerdict:
    """Return whether the next attempt may run, warning at 80% of a cap."""
    if limits.max_cost_usd is not None and limits.billing_mode != "max":
        cap = limits.max_cost_usd
        if cost_usd >= cap:
            return BudgetVerdict(BudgetState.EXCEEDED, f"cost ${cost_usd} exceeded budget ${cap}")
        if cost_usd >= cap * WARN_FRACTION:
            return BudgetVerdict(
                BudgetState.WARN, f"cost ${cost_usd} at {WARN_FRACTION:.0%} of ${cap}"
            )

    if limits.max_wall_seconds is not None:
        cap_s = limits.max_wall_seconds
        wall_s = wall_ms / 1000
        if wall_s >= cap_s:
            return BudgetVerdict(
                BudgetState.EXCEEDED, f"wall-clock {wall_s:.0f}s exceeded budget {cap_s}s"
            )
        if wall_s >= cap_s * float(WARN_FRACTION):
            return BudgetVerdict(
                BudgetState.WARN, f"wall-clock {wall_s:.0f}s at {WARN_FRACTION:.0%} of {cap_s}s"
            )

    return BudgetVerdict(BudgetState.OK)
