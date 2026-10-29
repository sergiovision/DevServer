"""Tests for the FastAPI application (main.py) and health endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def _patch_lifespan():
    """Patch all lifespan dependencies so the app doesn't need real services."""
    with (
        patch("main.start_consumer", new_callable=AsyncMock) as mock_start,
        patch("main.stop_consumer", new_callable=AsyncMock) as mock_stop,
        patch("main.start_scheduler", new_callable=AsyncMock) as mock_start_sched,
        patch("main.stop_scheduler", new_callable=AsyncMock) as mock_stop_sched,
        patch("main.embeddings.warm_up", new_callable=AsyncMock),
    ):
        yield {
            "start_consumer": mock_start,
            "stop_consumer": mock_stop,
            "start_scheduler": mock_start_sched,
            "stop_scheduler": mock_stop_sched,
        }


class TestHealthEndpoint:
    """Tests for the health check route."""

    async def test_health_returns_ok(self, _patch_lifespan):
        """GET /health returns status ok with service name."""
        from main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "devserver-worker"


class TestLifespan:
    """Tests for the FastAPI app lifespan (startup/shutdown)."""

    async def test_starts_and_stops_consumer(self, _patch_lifespan):
        """Lifespan starts the PgQueuer consumer on startup and stops it on shutdown."""
        from main import app, lifespan

        async with lifespan(app):
            _patch_lifespan["start_consumer"].assert_awaited_once()
            _patch_lifespan["stop_consumer"].assert_not_awaited()

        _patch_lifespan["stop_consumer"].assert_awaited_once()

    async def test_starts_and_stops_scheduler(self, _patch_lifespan):
        """Lifespan starts and stops the scheduler."""
        from main import app, lifespan

        async with lifespan(app):
            _patch_lifespan["start_scheduler"].assert_awaited_once()

        _patch_lifespan["stop_scheduler"].assert_awaited_once()
