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
        patch("main.resume_if_active", new_callable=AsyncMock) as mock_resume,
    ):
        yield {
            "start_consumer": mock_start,
            "stop_consumer": mock_stop,
            "start_scheduler": mock_start_sched,
            "stop_scheduler": mock_stop_sched,
            "resume_if_active": mock_resume,
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


class TestProRouteStartup:
    """Regression coverage for optional Pro-module import handling."""

    def test_license_status_route_is_mounted_in_pro_checkout(self):
        from main import _has_pro_routes, pro_router

        assert _has_pro_routes
        assert "/internal/license/status" in {
            route.path for route in pro_router.routes if hasattr(route, "path")
        }

    def test_nested_dependency_error_is_not_treated_as_free_edition(self):
        from main import _optional_module_is_absent

        exc = ModuleNotFoundError(
            "No module named 'cryptography.exceptions'",
            name="cryptography.exceptions",
        )

        assert not _optional_module_is_absent(exc, "services.pro.licensing")

    def test_missing_optional_parent_is_treated_as_free_edition(self):
        from main import _optional_module_is_absent

        exc = ModuleNotFoundError("No module named 'services.pro'", name="services.pro")

        assert _optional_module_is_absent(exc, "services.pro.licensing")


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

    async def test_resumes_night_cycle(self, _patch_lifespan):
        """Lifespan resumes active night cycle on startup."""
        from main import app, lifespan

        async with lifespan(app):
            _patch_lifespan["resume_if_active"].assert_awaited_once()
