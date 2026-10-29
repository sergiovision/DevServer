"""Seeding and inspection helpers shared by the runner tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from models.base import async_session

from .conftest import FAKE_VENDOR


async def seed_task(
    repo_path: Path,
    *,
    task_key: str = "DEMO-1",
    max_retries: int = 2,
    test_cmd: str = "",
    claude_model: str = "fake-primary",
    backup_model: str | None = None,
    max_cost_usd: Decimal | None = None,
    mode: str = "autonomous",
    git_flow: str = "patch",
    status: str = "queued",
    repo_name: str = "demo",
) -> int:
    """Insert a local-folder repo plus one queued task; return the task id."""
    async with async_session() as db:
        repo_id = (
            await db.execute(
                text(
                    """
                    INSERT INTO repos (name, gitea_url, gitea_owner, gitea_repo, clone_url,
                                       default_branch, test_cmd, max_retries, timeout_minutes,
                                       provider)
                    VALUES (:name, :root, '', '', '', 'main', :test_cmd, :retries, 5, 'local')
                    ON CONFLICT (name) DO UPDATE SET test_cmd = EXCLUDED.test_cmd
                    RETURNING id
                    """
                ),
                {
                    "name": repo_name,
                    "root": str(repo_path),
                    "test_cmd": test_cmd,
                    "retries": max_retries,
                },
            )
        ).scalar_one()
        task_id = (
            await db.execute(
                text(
                    """
                    INSERT INTO tasks (repo_id, task_key, title, description, status, mode,
                                       agent_vendor, claude_model, backup_model, max_cost_usd,
                                       git_flow, max_turns)
                    VALUES (:repo_id, :key, 'Add a feature', 'Write feature.txt', :status,
                            :mode, :vendor, :model, :backup, :max_cost, :git_flow, 10)
                    RETURNING id
                    """
                ),
                {
                    "repo_id": repo_id,
                    "key": task_key,
                    "mode": mode,
                    "vendor": FAKE_VENDOR,
                    "model": claude_model,
                    "backup": backup_model,
                    "max_cost": max_cost_usd,
                    "git_flow": git_flow,
                    "status": status,
                },
            )
        ).scalar_one()
        await db.commit()
    return task_id


async def event_types(task_id: int) -> list[str]:
    async with async_session() as db:
        rows = await db.execute(
            text("SELECT event_type FROM task_events WHERE task_id = :id ORDER BY id"),
            {"id": task_id},
        )
        return [row[0] for row in rows]


async def task_status(task_id: int) -> str:
    async with async_session() as db:
        return (
            await db.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id})
        ).scalar_one()


async def run_statuses(task_id: int) -> list[str]:
    async with async_session() as db:
        rows = await db.execute(
            text("SELECT status FROM task_runs WHERE task_id = :id ORDER BY id"), {"id": task_id}
        )
        return [row[0] for row in rows]


async def lock_owner(repo_name: str = "demo") -> str | None:
    async with async_session() as db:
        return (
            await db.execute(
                text("SELECT task_key FROM repo_locks WHERE repo_name = :name"),
                {"name": repo_name},
            )
        ).scalar_one_or_none()


def is_subsequence(expected: list[str], actual: list[str]) -> bool:
    """True when ``expected`` appears in ``actual`` in order (gaps allowed)."""
    remaining = iter(actual)
    return all(item in remaining for item in expected)
