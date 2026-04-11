"""Shared test fixtures for the DevServer worker test suite."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_settings():
    """Provide test settings that don't require real services.

    Patches settings in the queue_consumer module where it's used.
    """
    with patch("services.queue_consumer.settings") as mock:
        mock.database_url = "postgresql://test:test@localhost/test"
        mock.worker_concurrency = 1
        yield mock


@pytest.fixture
def mock_pgqueuer():
    """Create a mock PgQueuer instance."""
    mock_pgq = MagicMock()
    mock_pgq.run = AsyncMock()
    mock_pgq.entrypoint = MagicMock(return_value=lambda f: f)
    return mock_pgq


@pytest.fixture
def mock_job():
    """Create a mock PgQueuer job with configurable data."""
    job = MagicMock()
    job.id = 123
    job.payload = json.dumps({"taskId": "42", "claudeMode": "api", "maxTurns": None}).encode()
    return job


@pytest.fixture
def mock_job_no_task_id():
    """Create a mock PgQueuer job with no taskId."""
    job = MagicMock()
    job.id = 456
    job.payload = json.dumps({}).encode()
    return job


@pytest.fixture
def mock_job_no_payload():
    """Create a mock PgQueuer job with no payload."""
    job = MagicMock()
    job.id = 789
    job.payload = None
    return job
