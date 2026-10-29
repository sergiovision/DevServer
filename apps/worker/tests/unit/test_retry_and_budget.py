"""``services.retry`` and ``services.budget``: small pure helpers."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from services.budget import BudgetLimits, BudgetState, check_budget
from services.retry import Backoff, retry_async


class TestBackoff:
    def test_reuses_last_delay_beyond_schedule(self):
        backoff = Backoff(delays=(1, 2))
        assert [backoff.delay(i) for i in range(4)] == [1, 2, 2, 2]

    def test_jitter_stays_within_bound(self):
        backoff = Backoff(delays=(10,), jitter=5)
        assert all(10 <= backoff.delay(0) <= 15 for _ in range(50))

    def test_rejects_empty_schedule(self):
        with pytest.raises(ValueError):
            Backoff(delays=())


class TestRetryAsync:
    async def test_retries_until_result_is_acceptable(self):
        results = iter([429, 429, 200])
        calls = AsyncMock(side_effect=lambda: next(results))
        notices = []

        async def on_retry(notice):
            notices.append(notice.retry_number)

        with patch("services.retry.asyncio.sleep", AsyncMock()) as sleep:
            result = await retry_async(
                calls,
                attempts=5,
                backoff=Backoff((1, 2)),
                retry_on=lambda r: r == 429,
                on_retry=on_retry,
            )

        assert result == 200
        assert calls.await_count == 3
        assert notices == [1, 2]
        assert [c.args[0] for c in sleep.await_args_list] == [1, 2]

    async def test_returns_last_result_when_attempts_run_out(self):
        calls = AsyncMock(return_value=429)
        with patch("services.retry.asyncio.sleep", AsyncMock()):
            result = await retry_async(
                calls, attempts=3, backoff=Backoff((0,)), retry_on=lambda r: True
            )
        assert result == 429
        assert calls.await_count == 3

    async def test_exceptions_propagate_immediately(self):
        calls = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await retry_async(calls, attempts=3, backoff=Backoff((0,)), retry_on=lambda r: True)
        assert calls.await_count == 1


def _limits(cost=None, wall=None, mode="api"):
    return BudgetLimits(max_cost_usd=cost, max_wall_seconds=wall, billing_mode=mode)


class TestBudget:
    def test_no_caps_is_always_ok(self):
        verdict = check_budget(cost_usd=Decimal("999"), wall_ms=10**9, limits=_limits())
        assert verdict.state is BudgetState.OK

    def test_warns_at_eighty_percent_and_trips_at_cap(self):
        limits = _limits(cost=Decimal("1.00"))
        assert (
            check_budget(cost_usd=Decimal("0.79"), wall_ms=0, limits=limits).state is BudgetState.OK
        )
        assert (
            check_budget(cost_usd=Decimal("0.80"), wall_ms=0, limits=limits).state
            is BudgetState.WARN
        )
        assert (
            check_budget(cost_usd=Decimal("1.00"), wall_ms=0, limits=limits).state
            is BudgetState.EXCEEDED
        )

    def test_cost_cap_ignored_on_subscription_billing(self):
        limits = _limits(cost=Decimal("0.01"), mode="max")
        assert check_budget(cost_usd=Decimal("5"), wall_ms=0, limits=limits).state is BudgetState.OK

    def test_wall_clock_cap(self):
        limits = _limits(wall=10)
        verdict = check_budget(cost_usd=Decimal("0"), wall_ms=10_000, limits=limits)
        assert verdict.state is BudgetState.EXCEEDED
        assert "wall-clock" in verdict.reason
