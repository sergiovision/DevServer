"""The memory knowledge base against a real PostgreSQL schema.

Facts supersession, decisions, transcript archiving, the wake-up digest, the
HTTP lanes, and the prompt/env a task's agent receives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from models.base import async_session
from routes import memory_kb
from services.agent_runner import run_task
from services.repo_kb import RepoMemory

from .conftest import Scenario
from .support import seed_task

pytestmark = pytest.mark.usefixtures("db_engine", "quiet_world")


async def _repo_id(task_id: int) -> int:
    async with async_session() as db:
        return (
            await db.execute(text("SELECT repo_id FROM tasks WHERE id = :t"), {"t": task_id})
        ).scalar_one()


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(memory_kb.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://w") as c:
        yield c


async def test_fact_supersession_and_digest(git_repo: Path) -> None:
    task_id = await seed_task(git_repo, status="done")
    repo_id = await _repo_id(task_id)
    async with async_session() as db:
        kb = RepoMemory(db, repo_id)
        first = await kb.record_fact("tests", "run with", "make test", source_task_id=task_id)
        second = await kb.record_fact("tests", "run with", "pytest -q")
        assert first and second and first != second

        current = await kb.query_facts()
        assert [f["object"] for f in current] == ["pytest -q"]
        history = await kb.timeline("tests")
        assert [f["object"] for f in history] == ["make test", "pytest -q"]

        await kb.record_decision(problem="http", choice="httpx", reasoning="async support")
        digest = await kb.wake_up_digest(force=True)
        assert "- tests run with pytest -q" in digest
        assert "- chose httpx because async support" in digest
        assert "- tests run with make test" in digest  # under "No longer true"
        assert await kb.get_wake_digest() == digest

        assert await kb.invalidate_fact(second, reason="moved to nox")
        assert not await kb.invalidate_fact(second)
        assert await kb.query_facts() == []


async def test_archive_transcript_is_idempotent(git_repo: Path) -> None:
    task_id = await seed_task(git_repo, status="done")
    repo_id = await _repo_id(task_id)
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO task_runs (task_id, attempt, status, claude_output) "
                "VALUES (:t, 1, 'success', :out)"
            ),
            {"t": task_id, "out": "x" * 4500},
        )
        await db.commit()
        kb = RepoMemory(db, repo_id)
        assert await kb.archive_transcript(task_id, task_key="DEMO-1") == 3
        assert await kb.archive_transcript(task_id, task_key="DEMO-1") == 0
        # Transcripts stay out of default recall but are searchable on demand.
        assert all(m["memory_type"] != "transcript" for m in await kb.recall("x"))
        assert len(await kb.recall_transcripts("x", limit=5)) == 3


async def test_task_lanes_over_http(client: AsyncClient, git_repo: Path) -> None:
    task_id = await seed_task(git_repo, task_key="LANE-1")
    repo_id = await _repo_id(task_id)

    r = await client.post(
        "/internal/tasks/LANE-1/facts",
        json={"subject": "entry", "predicate": "is", "object": "src/main.py"},
    )
    assert r.status_code == 200 and r.json()["ok"]

    r = await client.post(
        "/internal/tasks/LANE-1/decisions",
        json={"problem": "queue", "choice": "pgqueuer", "alternatives": ["celery"]},
    )
    assert r.status_code == 200

    r = await client.post(
        "/internal/tasks/LANE-1/memory/remember", json={"content": "never push from tests"}
    )
    assert r.status_code == 200 and r.json()["memory_id"]

    facts = (await client.get("/internal/tasks/LANE-1/memory/facts")).json()
    assert [f["object"] for f in facts["facts"]] == ["src/main.py"]

    decisions = (
        await client.get("/internal/tasks/LANE-1/memory/decisions", params={"q": "pgqueuer"})
    ).json()
    assert decisions["count"] == 1
    assert decisions["decisions"][0]["metadata"]["choice"] == "pgqueuer"

    recall = (
        await client.get("/internal/tasks/LANE-1/memory/recall", params={"q": "push tests"})
    ).json()
    assert any("never push" in m["content"] for m in recall["memories"])

    rmap = (await client.get("/internal/tasks/LANE-1/repo-map")).json()
    assert rmap["ok"]

    assert (
        await client.get("/internal/tasks/NOPE/memory/recall", params={"q": "x"})
    ).status_code == 404

    resolved = (await client.get("/internal/repos/resolve", params={"path": str(git_repo)})).json()
    assert resolved["repo_id"] == repo_id

    async with async_session() as db:
        events = (
            (
                await db.execute(
                    text("SELECT event_type FROM task_events WHERE task_id = :t"), {"t": task_id}
                )
            )
            .scalars()
            .all()
        )
    assert {"fact_recorded", "decision_recorded", "memory_queried"} <= set(events)


async def test_console_writes_can_be_disabled(client: AsyncClient, git_repo: Path) -> None:
    repo_id = await _repo_id(await seed_task(git_repo))
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO settings (key, value) VALUES ('memory_allow_console_writes', "
                "CAST(:v AS JSONB))"
            ),
            {"v": json.dumps(False)},
        )
        await db.commit()
    r = await client.post(f"/internal/repos/{repo_id}/memory/remember", json={"content": "x"})
    assert r.status_code == 403


async def _set(key: str, value: object) -> None:
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS JSONB)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"k": key, "v": json.dumps(value)},
        )
        await db.commit()


async def test_agent_lanes_are_opt_in(scenario: Scenario, git_repo: Path) -> None:
    scenario.script({"exit": 0, "write": {"feature.txt": "hi\n"}, "session_id": "s1"})
    task_id = await seed_task(git_repo)

    assert await run_task(task_id) is True
    assert "## Autonomous agent lanes" not in scenario.calls[0]["prompt"]


async def test_agent_gets_lanes_in_prompt(scenario: Scenario, git_repo: Path) -> None:
    scenario.script({"exit": 0, "write": {"feature.txt": "hi\n"}, "session_id": "s1"})
    task_id = await seed_task(git_repo)
    await _set("agent_lanes_prompt", True)

    assert await run_task(task_id) is True

    first_prompt = scenario.calls[0]["prompt"]
    assert "## Autonomous agent lanes" in first_prompt
    assert "$DEVSERVER_TASK_KEY/memory/recall" in first_prompt
