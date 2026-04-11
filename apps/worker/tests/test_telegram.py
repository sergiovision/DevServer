"""Tests for telegram service — local and external controller switching."""

import sys
import os

import pytest
from unittest.mock import AsyncMock, patch

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"
os.environ["TELEGRAM_CONTROLLER_URL"] = "http://tg-controller:8200"
os.environ["TELEGRAM_CONTROLLER_TOKEN"] = "secret123"

from services import telegram
from config import settings


# ─── Mode switching ──────────────────────────────────────────────────────────

def test_use_external_when_mode_is_external():
    orig = settings.telegram_controller_mode
    settings.telegram_controller_mode = "external"
    try:
        assert telegram._use_external() is True
    finally:
        settings.telegram_controller_mode = orig


def test_use_local_when_mode_is_local():
    orig = settings.telegram_controller_mode
    settings.telegram_controller_mode = "local"
    try:
        assert telegram._use_external() is False
    finally:
        settings.telegram_controller_mode = orig


# ─── Local mode tests ────────────────────────────────────────────────────────

@pytest.mark.anyio
@patch("services.telegram.httpx.AsyncClient")
async def test_local_send_success(mock_client_cls):
    settings.telegram_controller_mode = "local"

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

    settings.telegram_controller_mode = "external"


@pytest.mark.anyio
@patch("services.telegram.httpx.AsyncClient")
async def test_local_send_failure(mock_client_cls):
    settings.telegram_controller_mode = "local"

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

    settings.telegram_controller_mode = "external"


@pytest.mark.anyio
async def test_local_send_no_token():
    settings.telegram_controller_mode = "local"
    orig_token = settings.telegram_bot_token
    settings.telegram_bot_token = ""

    result = await telegram.tg_send("test message")
    assert result is False

    settings.telegram_bot_token = orig_token
    settings.telegram_controller_mode = "external"


# ─── External mode tests ────────────────────────────────────────────────────

@pytest.mark.anyio
@patch("services.telegram.httpx.AsyncClient")
async def test_external_send_success(mock_client_cls):
    settings.telegram_controller_mode = "external"

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True}
    mock_instance = AsyncMock()
    mock_instance.post.return_value = mock_resp
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_instance

    result = await telegram.tg_send("external test")
    assert result is True

    call_args = mock_instance.post.call_args
    assert "tg-controller:8200/send" in call_args[0][0]
    assert call_args[1]["json"]["message"] == "external test"
    assert call_args[1]["headers"]["Authorization"] == "Bearer secret123"


@pytest.mark.anyio
@patch("services.telegram.httpx.AsyncClient")
async def test_external_send_failure(mock_client_cls):
    settings.telegram_controller_mode = "external"

    mock_resp = AsyncMock()
    mock_resp.status_code = 502
    mock_resp.text = "Bad Gateway"
    mock_instance = AsyncMock()
    mock_instance.post.return_value = mock_resp
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_instance

    result = await telegram.tg_send("test")
    assert result is False


@pytest.mark.anyio
@patch("services.telegram.httpx.AsyncClient")
async def test_external_send_connection_error(mock_client_cls):
    settings.telegram_controller_mode = "external"

    mock_instance = AsyncMock()
    mock_instance.post.side_effect = Exception("Connection refused")
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_instance

    result = await telegram.tg_send("test")
    assert result is False


# ─── Headers ─────────────────────────────────────────────────────────────────

def test_ext_headers_with_token():
    headers = telegram._ext_headers()
    assert headers["Authorization"] == "Bearer secret123"
    assert headers["Content-Type"] == "application/json"


def test_ext_headers_without_token():
    orig = settings.telegram_controller_token
    settings.telegram_controller_token = ""
    headers = telegram._ext_headers()
    assert "Authorization" not in headers
    settings.telegram_controller_token = orig
