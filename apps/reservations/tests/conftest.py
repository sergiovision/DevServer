"""Test fixtures: a real Postgres (docker-compose, port 5440), fresh tables per test.

Tests use a separate ``reservations_test`` database (created on demand and
migrated from ``migrations/001_init.sql``) so they never wipe the dev data in
``reservations``. Override with ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from helpers import TEST_DATABASE_URL, make_settings
from reservations.api.app import create_app
from reservations.worker.main import WorkerHandle, start_worker

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_init.sql"
TABLES = (
    "webhook_deliveries, reservations, reservation_batch_items, reservation_batches, "
    "properties, rate_limit_counters, pgqueuer, pgqueuer_log, pgqueuer_statistics"
)


async def _connect_with_retry(dsn: str, attempts: int = 30) -> asyncpg.Connection:
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn)
        except OSError, asyncpg.CannotConnectNowError:
            if i == attempts - 1:
                raise
            await asyncio.sleep(1)
    raise AssertionError("unreachable")


@pytest.fixture(scope="session")
async def database_url() -> str:
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[1].split("?")[0]
    admin_dsn = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    admin = await _connect_with_retry(admin_dsn)
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        if await conn.fetchval("SELECT to_regclass('public.reservations')") is None:
            await conn.execute(MIGRATION.read_text())
    finally:
        await conn.close()
    return TEST_DATABASE_URL


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database_url)
    await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    try:
        yield conn
    finally:
        await conn.close()


ApiFactory = Callable[..., Awaitable[httpx.AsyncClient]]


@pytest.fixture
async def api(db: asyncpg.Connection) -> AsyncIterator[ApiFactory]:
    """``await api(**settings_overrides)`` -> httpx client bound to a fresh app."""
    stack: list[tuple[Any, httpx.AsyncClient]] = []

    async def factory(**overrides: Any) -> httpx.AsyncClient:
        app = create_app(make_settings(**overrides))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30
        )
        stack.append((lifespan, client))
        return client

    yield factory
    for lifespan, client in reversed(stack):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


WorkerFactory = Callable[..., Awaitable[WorkerHandle]]


@pytest.fixture
async def workers(db: asyncpg.Connection) -> AsyncIterator[WorkerFactory]:
    """``await workers(**settings_overrides)`` starts one in-process worker."""
    handles: list[WorkerHandle] = []

    async def factory(**overrides: Any) -> WorkerHandle:
        handle = await start_worker(make_settings(**overrides))
        handles.append(handle)
        return handle

    yield factory
    await asyncio.gather(*(h.stop() for h in handles))
