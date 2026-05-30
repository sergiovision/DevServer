"""Outcome prediction (free tier) — repo-level forecast, no embeddings.

The Pro edition forecasts from *similar* past tasks via pgvector
(``services/pro/memory.predict_outcome``). The free edition has no
embeddings, but it does have ``task_runs``, so we fall back to a
repo-level baseline: across this repo's tasks that have actually run,
what share succeeded, and how long / how many turns did they take.

Returned shape matches the Pro predictor so one UI renders both:
``{sample_size, success_probability, avg_duration_ms, avg_turns,
similar, basis}``. ``basis='repo'`` lets the UI label it as a baseline
rather than a similarity match (Pro returns ``basis='similar'``).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def predict_outcome_basic(session: AsyncSession, repo_id: int) -> dict | None:
    """Repo-level success/duration baseline from ``task_runs``. Never raises.

    A task counts as "succeeded" if any of its runs reached ``status='success'``
    (the terminal success state the agent runner writes). Only tasks that have
    at least one run are considered, so freshly-created tasks don't dilute the
    rate.
    """
    try:
        row = (await session.execute(
            text(
                """
                WITH per_task AS (
                    SELECT t.id,
                           BOOL_OR(tr.status = 'success') AS succeeded,
                           COALESCE(SUM(tr.duration_ms), 0) AS dur,
                           COALESCE(SUM(tr.turns), 0)       AS turns
                    FROM tasks t
                    JOIN task_runs tr ON tr.task_id = t.id
                    WHERE t.repo_id = :repo_id
                    GROUP BY t.id
                )
                SELECT COUNT(*)                                  AS n,
                       COUNT(*) FILTER (WHERE succeeded)         AS ok,
                       COALESCE(AVG(dur), 0)                     AS avg_dur,
                       COALESCE(AVG(turns), 0)                   AS avg_turns
                FROM per_task
                """
            ),
            {"repo_id": repo_id},
        )).fetchone()

        n = int(row[0] or 0)
        if n == 0:
            return {"sample_size": 0, "success_probability": None,
                    "avg_duration_ms": 0, "avg_turns": 0, "similar": [],
                    "basis": "repo"}
        return {
            "sample_size": n,
            "success_probability": round(int(row[1] or 0) / n, 2),
            "avg_duration_ms": int(row[2] or 0),
            "avg_turns": int(row[3] or 0),
            "similar": [],
            "basis": "repo",
        }
    except Exception:
        logger.exception("predict_outcome_basic failed")
        return None
