"""Tests for the PgQueuer queue consumer (services/queue_consumer.py)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.queue_consumer import (
    start_consumer,
    stop_consumer,
    get_consumer,
    is_consumer_running,
)


class TestStartConsumer:
    """Tests for starting the PgQueuer consumer."""

    @patch("services.queue_consumer._create_pgqueuer", new_callable=AsyncMock)
    async def test_start_consumer_creates_pgqueuer(self, mock_create):
        """start_consumer creates a PgQueuer instance and starts it."""
        import services.queue_consumer as mod
        mod._pgq = None
        mod._runner_task = None

        mock_pgq = MagicMock()
        mock_pgq.run = AsyncMock()
        mock_create.return_value = mock_pgq

        await start_consumer()

        mock_create.assert_awaited_once()
        assert mod._pgq is mock_pgq
        assert mod._runner_task is not None

        # Cleanup
        mod._runner_task.cancel()
        try:
            await mod._runner_task
        except asyncio.CancelledError:
            pass
        mod._pgq = None
        mod._runner_task = None


class TestStopConsumer:
    """Tests for stopping the PgQueuer consumer."""

    @patch("services.queue_consumer._create_pgqueuer", new_callable=AsyncMock)
    async def test_stop_consumer_cancels_task(self, mock_create):
        """stop_consumer cancels the runner task and clears state."""
        import services.queue_consumer as mod

        mock_pgq = MagicMock()
        # Make run() block forever until cancelled
        async def mock_run():
            await asyncio.sleep(3600)
        mock_pgq.run = mock_run
        mock_create.return_value = mock_pgq

        await start_consumer()
        assert mod._runner_task is not None
        assert mod._pgq is not None

        await stop_consumer()

        assert mod._pgq is None
        assert mod._runner_task is None

    async def test_stop_consumer_noop_when_not_running(self):
        """stop_consumer is safe to call when nothing is running."""
        import services.queue_consumer as mod
        mod._pgq = None
        mod._runner_task = None

        await stop_consumer()  # Should not raise

        assert mod._pgq is None
        assert mod._runner_task is None


class TestGetConsumer:
    """Tests for the get_consumer accessor."""

    def test_get_consumer_returns_instance(self, mock_pgqueuer):
        """get_consumer returns the current PgQueuer when one exists."""
        import services.queue_consumer as mod
        mod._pgq = mock_pgqueuer

        assert get_consumer() is mock_pgqueuer
        mod._pgq = None

    def test_get_consumer_returns_none(self):
        """get_consumer returns None when no consumer is running."""
        import services.queue_consumer as mod
        mod._pgq = None

        assert get_consumer() is None


class TestIsConsumerRunning:
    """Tests for the is_consumer_running check."""

    def test_returns_false_when_no_task(self):
        """Returns False when no runner task exists."""
        import services.queue_consumer as mod
        mod._runner_task = None

        assert is_consumer_running() is False

    def test_returns_true_when_task_running(self):
        """Returns True when runner task is active."""
        import services.queue_consumer as mod
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mod._runner_task = mock_task

        assert is_consumer_running() is True
        mod._runner_task = None

    def test_returns_false_when_task_done(self):
        """Returns False when runner task has completed."""
        import services.queue_consumer as mod
        mock_task = MagicMock()
        mock_task.done.return_value = True
        mod._runner_task = mock_task

        assert is_consumer_running() is False
        mod._runner_task = None
