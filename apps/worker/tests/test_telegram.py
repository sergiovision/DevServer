"""Tests for telegram service — direct Bot API."""

import sys
import os

import pytest
from unittest.mock import AsyncMock, patch

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"

from services import telegram
from config import settings


# ─── Send tests ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
@patch("services.telegram.httpx.AsyncClient")
async def test_send_success(mock_client_cls):
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_instance = AsyncMock()
    mock_instance.post.return_value = mock_resp
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_instance

    result = await telegram.tg_send("test message")
    assert result is True

    call_args = mock_instance.post.call_args
    assert "sendMessage" in call_args[0][0]
    assert call_args[1]["data"]["text"] == "test message"


@pytest.mark.anyio
@patch("services.telegram.httpx.AsyncClient")
async def test_send_failure(mock_client_cls):
    mock_resp = AsyncMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    mock_instance = AsyncMock()
    mock_instance.post.return_value = mock_resp
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_instance

    result = await telegram.tg_send("test message")
    assert result is False


@pytest.mark.anyio
async def test_send_no_token():
    orig_token = settings.telegram_bot_token
    settings.telegram_bot_token = ""

    result = await telegram.tg_send("test message")
    assert result is False

    settings.telegram_bot_token = orig_token
