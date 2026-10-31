"""Fixtures for the ``run_task`` characterisation tests.

These tests drive the real runner end to end: a real PostgreSQL schema, a
real git repository and a real subprocess (``fake_agent.py``) standing in for
the vendor CLI. Only the outside world is stubbed — notifications and the
embedding model.

The database comes from ``DEVSERVER_TEST_DATABASE_URL`` (default
``postgresql:///devserver_test``). It is created and migrated on first use;
every test starts from empty tables. When no server is reachable the whole
module is skipped rather than failed, so the unit tests still run anywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from config import settings
from models import base
from services import agent_backends, embeddings
from services.agent_backends import AgentBackend, AgentResult
from services.notify import notify

TEST_DSN = os.environ.get("DEVSERVER_TEST_DATABASE_URL", "postgresql:///devserver_test")
MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "database" / "migrations"
FAKE_AGENT = Path(__file__).with_name("fake_agent.py")
FAKE_VENDOR = "fake"

_TABLES_TO_RESET = (
    "task_messages",
    "task_events",
    "task_runs",
    "tasks",
    "repo_locks",
    "daily_stats",
    "agent_memory",
    "repos",
    "settings",
    "webhook_triggers",
)


# ── Database ────────────────────────────────────────────────────────────────


async def _create_and_migrate() -> None:
    db_name = TEST_DSN.rsplit("/", 1)[-1]
    admin = await asyncpg.connect(TEST_DSN.rsplit("/", 1)[0] + "/postgres")
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    conn = await asyncpg.connect(TEST_DSN)
    try:
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(migration.read_text())
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    try:
        asyncio.run(_create_and_migrate())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL test database unavailable ({TEST_DSN}): {exc}")
    return TEST_DSN


@pytest.fixture
async def db_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """Point the app's session factory at the test database, tables emptied."""
    url = migrated_database.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES_TO_RESET)} RESTART IDENTITY CASCADE"))
    original_bind = base.async_session.kw.get("bind")
    base.async_session.configure(bind=engine)
    try:
        yield engine
    finally:
        base.async_session.configure(bind=original_bind)
        await engine.dispose()


# ── Fake agent backend ──────────────────────────────────────────────────────


class FakeBackend(AgentBackend):
    """Runs ``fake_agent.py`` with a per-test scenario file."""

    vendor = FAKE_VENDOR
    label = "Fake"
    cli_bin = sys.executable

    def __init__(self, scenario_path: Path) -> None:
        self.scenario_path = scenario_path

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        allowed_tools: str,
        session_id: str | None,
        max_turns: int | None,
        mcp_config_path: str | None = None,
        stream_json: bool = False,
        extra_args: list[str] | None = None,
    ) -> list[str]:
        return [sys.executable, str(FAKE_AGENT), str(self.scenario_path), model, prompt]

    def parse_output(self, raw_output: str, prior_session_id: str | None) -> AgentResult:
        data = json.loads(raw_output.strip().splitlines()[-1])
        return AgentResult(
            result=data["result"],
            cost_usd=data["cost_usd"],
            num_turns=data["num_turns"],
            session_id=data["session_id"] or prior_session_id,
            raw_output=raw_output,
            subtype=data["subtype"],
            errors=data["errors"],
        )


@dataclass
class Scenario:
    """The scripted agent behaviour for one test, plus what it observed."""

    path: Path

    def script(self, *steps: dict) -> None:
        self.path.write_text(json.dumps(list(steps)))

    @property
    def calls(self) -> list[dict]:
        calls_path = self.path.with_suffix(".calls.jsonl")
        if not calls_path.exists():
            return []
        return [json.loads(line) for line in calls_path.read_text().splitlines()]


@pytest.fixture
def scenario(tmp_path: Path) -> Iterator[Scenario]:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps([{"exit": 0}]))
    previous = agent_backends.register_backend(FakeBackend(path))
    try:
        yield Scenario(path)
    finally:
        agent_backends.unregister_backend(FAKE_VENDOR)
        if previous is not None:
            agent_backends.register_backend(previous)


# ── Outside world ───────────────────────────────────────────────────────────


@pytest.fixture
def quiet_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No notifications, no embedding model, a fixed git identity, logs under ``tmp_path``."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(settings, "log_dir", str(log_dir))
    # The runner commits with this identity; without a .env it is empty and
    # every commit (and so every patch export) silently fails.
    monkeypatch.setattr(settings, "git_user_name", "DevServer Test")
    monkeypatch.setattr(settings, "git_user_email", "test@example.com")
    monkeypatch.setattr(notify, "_backends", [])

    async def _no_embedding(_text: str) -> None:
        return None

    monkeypatch.setattr(embeddings, "embed", _no_embedding)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A local git repository on ``main`` with one commit and a clean tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo
