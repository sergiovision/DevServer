"""Pure parts of ``services.memory`` (recency decay) and ``services.outcome``."""

from datetime import UTC, datetime, timedelta

import pytest

from services.memory import _decay_factor
from services.outcome import summarise_similar

NOW = datetime(2026, 11, 11, tzinfo=UTC)


class TestDecayFactor:
    def test_disabled_when_half_life_is_zero(self):
        assert _decay_factor(NOW - timedelta(days=400), 0, now=NOW) == 1.0

    def test_missing_timestamp_is_not_penalised(self):
        assert _decay_factor(None, 90, now=NOW) == 1.0

    def test_halves_every_half_life(self):
        assert _decay_factor(NOW, 90, now=NOW) == 1.0
        assert _decay_factor(NOW - timedelta(days=90), 90, now=NOW) == pytest.approx(0.5)
        assert _decay_factor(NOW - timedelta(days=180), 90, now=NOW) == pytest.approx(0.25)

    def test_naive_timestamps_are_read_as_utc(self):
        naive = (NOW - timedelta(days=90)).replace(tzinfo=None)
        assert _decay_factor(naive, 90, now=NOW) == pytest.approx(0.5)

    def test_future_timestamp_never_boosts(self):
        assert _decay_factor(NOW + timedelta(days=5), 90, now=NOW) == 1.0


def _run(task_id: int, *, succeeded: bool, duration_ms: int = 1000, turns: int = 10) -> dict:
    return {
        "id": task_id,
        "task_key": f"T-{task_id}",
        "title": f"task {task_id}",
        "status": "done" if succeeded else "failed",
        "succeeded": succeeded,
        "duration_ms": duration_ms,
        "turns": turns,
    }


class TestSummariseSimilar:
    def test_no_history_is_an_empty_similar_forecast(self):
        forecast = summarise_similar([], {})

        assert forecast["sample_size"] == 0
        assert forecast["success_probability"] is None
        assert forecast["basis"] == "similar"

    def test_aggregates_runs_and_orders_by_similarity(self):
        runs = [
            _run(1, succeeded=True, duration_ms=1000, turns=10),
            _run(2, succeeded=False, duration_ms=3000, turns=30),
            _run(3, succeeded=True, duration_ms=2000, turns=20),
        ]

        forecast = summarise_similar(runs, {1: 0.61, 2: 0.9, 3: 0.75})

        assert forecast["sample_size"] == 3
        assert forecast["success_probability"] == 0.67
        assert forecast["avg_duration_ms"] == 2000
        assert forecast["avg_turns"] == 20
        assert [s["task_key"] for s in forecast["similar"]] == ["T-2", "T-3", "T-1"]
        assert forecast["similar"][0] == {
            "task_key": "T-2",
            "title": "task 2",
            "status": "failed",
            "succeeded": False,
            "similarity": 0.9,
        }
