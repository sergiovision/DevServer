"""Outcome prediction: forecast a task from what already ran in its repo.

Two forecasts share one shape, ``{sample_size, success_probability,
avg_duration_ms, avg_turns, similar, basis}``:

    - ``predict_outcome_similar`` (``basis='similar'``) — the k past tasks
      whose stored memories are closest to this task's title + description
    - ``predict_outcome_basic``   (``basis='repo'``)    — the repo-wide
      baseline, used when there is no similar-task signal

``basis`` lets the UI label where the number comes from.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services import embeddings

logger = logging.getLogger(__name__)

#: How many nearest past tasks a similarity forecast is averaged over.
SIMILAR_TASKS_K = 5


def _empty(basis: str) -> dict:
    return {
        "sample_size": 0,
        "success_probability": None,
        "avg_duration_ms": 0,
        "avg_turns": 0,
        "similar": [],
        "basis": basis,
    }


def summarise_similar(runs: list[dict], similarity: dict[int, float]) -> dict:
    """Aggregate per-task run totals into a ``basis='similar'`` forecast.

    ``runs`` holds one dict per past task: ``id, task_key, title, status,
    succeeded, duration_ms, turns``; ``similarity`` maps task id → cosine
    similarity to the task being forecast.
    """
    if not runs:
        return _empty("similar")
    n = len(runs)
    similar = sorted(
        (
            {
                "task_key": r["task_key"],
                "title": r["title"],
                "status": r["status"],
                "succeeded": bool(r["succeeded"]),
                "similarity": round(similarity.get(r["id"], 0.0), 3),
            }
            for r in runs
        ),
        key=lambda s: s["similarity"],
        reverse=True,
    )
    return {
        "sample_size": n,
        "success_probability": round(sum(1 for r in runs if r["succeeded"]) / n, 2),
        "avg_duration_ms": int(sum(int(r["duration_ms"] or 0) for r in runs) / n),
        "avg_turns": int(sum(int(r["turns"] or 0) for r in runs) / n),
        "similar": similar,
        "basis": "similar",
    }


async def predict_outcome_similar(
    session: AsyncSession,
    repo_id: int,
    task_id: int,
    title: str,
    description: str,
    k: int = SIMILAR_TASKS_K,
) -> dict | None:
    """Forecast from the ``k`` most similar past tasks in the repo. Never raises.

    Embeds the task, finds the nearest prior tasks through their stored
    ``agent_memory`` rows, and aggregates those tasks' real run outcomes.
    Returns ``None`` when no embedding is available, and a forecast with
    ``sample_size=0`` when the repo has no comparable history yet.
    """
    try:
        embedding = await embeddings.embed(f"{title}\n{description}".strip())
        if embedding is None:
            return None

        sim_rows = (
            await session.execute(
                text(
                    """
                SELECT task_id, MAX(1 - (embedding <=> CAST(:emb AS vector))) AS sim
                FROM agent_memory
                WHERE repo_id = :repo_id
                  AND task_id IS NOT NULL
                  AND task_id <> :task_id
                  AND embedding IS NOT NULL
                  AND memory_type <> 'transcript'
                  AND invalidated_at IS NULL
                GROUP BY task_id
                ORDER BY sim DESC
                LIMIT :k
                """
                ),
                {"repo_id": repo_id, "task_id": task_id, "emb": str(embedding), "k": k},
            )
        ).fetchall()
        if not sim_rows:
            return _empty("similar")
        similarity = {int(r[0]): float(r[1]) for r in sim_rows}

        run_rows = (
            await session.execute(
                text(
                    """
                SELECT t.id, t.task_key, t.title, t.status,
                       BOOL_OR(tr.status = 'success')   AS succeeded,
                       COALESCE(SUM(tr.duration_ms), 0) AS duration_ms,
                       COALESCE(SUM(tr.turns), 0)       AS turns
                FROM tasks t
                JOIN task_runs tr ON tr.task_id = t.id
                WHERE t.id = ANY(:ids)
                GROUP BY t.id, t.task_key, t.title, t.status
                """
                ),
                {"ids": list(similarity)},
            )
        ).fetchall()
        return summarise_similar([dict(r._mapping) for r in run_rows], similarity)
    except Exception:
        logger.exception("predict_outcome_similar failed")
        await session.rollback()
        return None


async def predict_outcome_basic(session: AsyncSession, repo_id: int) -> dict | None:
    """Repo-level success/duration baseline from ``task_runs``. Never raises.

    A task counts as "succeeded" if any of its runs reached ``status='success'``
    (the terminal success state the agent runner writes). Only tasks that have
    at least one run are considered, so freshly-created tasks don't dilute the
    rate.
    """
    try:
        row = (
            await session.execute(
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
            )
        ).fetchone()

        n = int(row[0] or 0)
        if n == 0:
            return _empty("repo")
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
