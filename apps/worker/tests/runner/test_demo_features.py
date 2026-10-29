"""End-to-end tests for the demo features: Plan Gate, Task Notes, Patch Export."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from models.base import async_session
from routes import task_artifacts
from services import plan_gate, task_notes
from services.agent_runner import run_task

from .conftest import Scenario
from .support import event_types, seed_task, task_status

pytestmark = pytest.mark.usefixtures("db_engine", "quiet_world")

_PLAN = {
    "summary": "Write feature.txt",
    "steps": ["create the file"],
    "files_to_touch": ["feature.txt"],
    "risks": [],
}


async def _decide_when_pending(task_id: int, column: str) -> None:
    async with asyncio.timeout(10):
        while "plan_pending" not in await event_types(task_id):
            await asyncio.sleep(0.05)
    async with async_session() as db:
        await db.execute(text(f"UPDATE tasks SET {column} = NOW() WHERE id = :id"), {"id": task_id})
        await db.commit()


@pytest.fixture
def fast_plan_gate(monkeypatch):
    monkeypatch.setattr(plan_gate, "POLL_SECONDS", 0.05)


@pytest.mark.usefixtures("fast_plan_gate")
class TestPlanGate:
    async def test_approved_plan_reaches_the_prompt_and_limits_scope(
        self, scenario: Scenario, git_repo: Path
    ):
        scenario.script(
            {"exit": 0, "result": json.dumps(_PLAN)},
            {"exit": 0, "write": {"feature.txt": "x\n"}},
        )
        task_id = await seed_task(git_repo, mode="interactive")

        approver = asyncio.create_task(_decide_when_pending(task_id, "plan_approved_at"))
        assert await run_task(task_id) is True
        await approver

        assert "## Approved Plan" in scenario.calls[1]["prompt"]
        assert await task_status(task_id) == "test"
        events = await event_types(task_id)
        assert events.index("plan_pending") < events.index("plan_approved")
        async with async_session() as db:
            stored = (
                await db.execute(
                    text("SELECT plan_json FROM task_runs WHERE task_id = :id AND attempt = 0"),
                    {"id": task_id},
                )
            ).scalar_one()
        assert stored["files_to_touch"] == ["feature.txt"]

    async def test_rejected_plan_blocks_without_implementing(
        self, scenario: Scenario, git_repo: Path
    ):
        scenario.script({"exit": 0, "result": json.dumps(_PLAN)})
        task_id = await seed_task(git_repo, mode="interactive")

        rejecter = asyncio.create_task(_decide_when_pending(task_id, "plan_rejected_at"))
        assert await run_task(task_id) is False
        await rejecter

        assert len(scenario.calls) == 1
        assert await task_status(task_id) == "blocked"
        assert "plan_rejected" in await event_types(task_id)

    async def test_timeout_blocks_the_task(self, scenario: Scenario, git_repo: Path, monkeypatch):
        monkeypatch.setattr(plan_gate, "DEFAULT_APPROVAL_TIMEOUT_SECONDS", 0.2)
        scenario.script({"exit": 0, "result": json.dumps(_PLAN)})
        task_id = await seed_task(git_repo, mode="interactive")

        assert await run_task(task_id) is False

        assert await task_status(task_id) == "blocked"


class TestTaskNotes:
    async def test_note_is_injected_into_exactly_one_attempt(
        self, scenario: Scenario, git_repo: Path
    ):
        scenario.script(
            {"exit": 0, "session_id": "s1"},
            {"exit": 0, "write": {"ok.txt": "x\n"}, "session_id": "s1"},
        )
        task_id = await seed_task(git_repo, test_cmd="test -f ok.txt")
        async with async_session() as db:
            note = await task_notes.post_note(db, task_key="DEMO-1", body="Prefer small commits")
        assert note.will_be_delivered

        assert await run_task(task_id) is True

        prompts = [call["prompt"] for call in scenario.calls]
        assert ["Prefer small commits" in p for p in prompts] == [True, False]

    async def test_note_on_finished_task_is_stored_but_never_injected(self, git_repo: Path):
        task_id = await seed_task(git_repo, status="test")
        async with async_session() as db:
            note = await task_notes.post_note(db, task_key="DEMO-1", body="too late")
            assert not note.will_be_delivered
            assert [n["body"] for n in await task_notes.list_notes(db, task_key="DEMO-1")] == [
                "too late"
            ]
            assert await task_notes.take_unread(db, task_id=task_id) == []


class TestPatchExportFlow:
    async def test_patch_mode_success_exposes_downloadable_patches(
        self, scenario: Scenario, git_repo: Path
    ):
        scenario.script({"exit": 0, "write": {"feature.txt": "x\n"}})
        task_id = await seed_task(git_repo, git_flow="patch")
        assert await run_task(task_id) is True
        assert "patches_generated" in await event_types(task_id)

        transport = ASGITransport(app=_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            listing = (await client.get("/internal/tasks/DEMO-1/patches")).json()["patches"]
            assert len(listing) == 1
            download = await client.get(f"/internal/tasks/DEMO-1/patches/{listing[0]['name']}")
            assert download.status_code == 200
            assert "feature.txt" in download.text
            traversal = await client.get("/internal/tasks/DEMO-1/patches/..%2F..%2Fsecret.patch")
            assert traversal.status_code == 400
            bad_name = await client.get("/internal/tasks/DEMO-1/patches/secret.txt")
            assert bad_name.status_code == 400


def _app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(task_artifacts.router)
    return app
