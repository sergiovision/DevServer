"""Unit tests for DevServer's direct OpenAI API and streaming adapters."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from services import llm_client
from services.llm_stream import _translate_openai


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-6-astra", True),
        ("gpt-5.6-sol", True),
        ("o1", True),
        ("o3-mini", True),
        ("o4-mini", True),
        ("gpt-4o", False),
        ("gpt-4.1", False),
    ],
)
def test_openai_completion_token_parameter_detection(model, expected):
    assert llm_client._openai_uses_completion_tokens(model) is expected


def test_openai_modern_chat_request_contract():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_task",
                "description": "Look up a task",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with patch.object(llm_client.settings, "openai_base_url", ""):
        url, headers, body = llm_client._build_openai_request(
            "test-key",
            "gpt-5.6-sol",
            [{"role": "user", "content": "Hello"}],
            2048,
            True,
            system="Be concise.",
            stream=True,
            tools=tools,
        )

    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert body["model"] == "gpt-5.6-sol"
    assert body["messages"] == [
        {"role": "developer", "content": "Be concise."},
        {"role": "user", "content": "Hello"},
    ]
    assert body["max_completion_tokens"] == 2048
    assert "max_tokens" not in body
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["tools"] == tools


def test_openai_legacy_chat_request_keeps_legacy_fields():
    with patch.object(llm_client.settings, "openai_base_url", ""):
        _, _, body = llm_client._build_openai_request(
            "test-key",
            "gpt-4o",
            [{"role": "user", "content": "Hello"}],
            512,
            system="Be concise.",
        )

    assert body["messages"][0] == {"role": "system", "content": "Be concise."}
    assert body["max_tokens"] == 512
    assert "max_completion_tokens" not in body


def test_openai_azure_request_contract():
    with patch.object(
        llm_client.settings,
        "openai_base_url",
        "https://example.openai.azure.com/",
    ), patch.object(llm_client.settings, "openai_api_version", "2026-01-01-preview"):
        url, headers, body = llm_client._build_openai_request(
            "azure-key",
            "deployment-name",
            [{"role": "user", "content": "Hello"}],
            1024,
            system="Follow policy.",
        )

    assert url == (
        "https://example.openai.azure.com/openai/deployments/deployment-name/"
        "chat/completions?api-version=2026-01-01-preview"
    )
    assert headers == {"api-key": "azure-key", "Content-Type": "application/json"}
    assert "model" not in body
    assert body["max_completion_tokens"] == 1024
    assert body["messages"][0] == {"role": "developer", "content": "Follow policy."}


def test_openai_response_parser_handles_content_and_empty_choices():
    assert llm_client._parse_openai(
        {"choices": [{"message": {"content": "Completed"}}]}
    ) == "Completed"
    assert llm_client._parse_openai({"choices": []}) == ""


def test_openai_api_key_resolution_is_actionable():
    with patch.object(llm_client.settings, "openai_api_key", ""):
        with pytest.raises(ValueError, match="OPENAI_API_KEY is not set"):
            llm_client.resolve_api_key("openai")


@pytest.mark.asyncio
async def test_openai_complete_dispatches_api_mode_without_network():
    completion = AsyncMock(return_value="API answer")
    with patch.object(llm_client, "_complete_via_http", completion):
        result = await llm_client.complete(
            vendor="openai",
            model="gpt-5.6-sol",
            prompt="Answer this",
            max_tokens=300,
            timeout=15,
            json_mode=True,
            mode="api",
        )

    assert result == "API answer"
    completion.assert_awaited_once_with(
        vendor="openai",
        model="gpt-5.6-sol",
        messages=[{"role": "user", "content": "Answer this"}],
        system=None,
        max_tokens=300,
        timeout=15,
        json_mode=True,
    )


@pytest.mark.asyncio
async def test_openai_complete_dispatches_subscription_mode_to_codex():
    completion = AsyncMock(return_value="Codex answer")
    with patch.object(llm_client, "_complete_via_cli", completion):
        result = await llm_client.complete(
            vendor="openai",
            model="gpt-5.6-sol",
            prompt="Answer this",
            timeout=20,
            mode="max",
        )

    assert result == "Codex answer"
    completion.assert_awaited_once_with(
        vendor="openai",
        model="gpt-5.6-sol",
        prompt="Answer this",
        timeout=20,
    )


@pytest.mark.asyncio
async def test_openai_stream_translation_text_tools_and_usage():
    async def frames():
        payloads = [
            {
                "choices": [
                    {
                        "delta": {
                            "content": "Hello ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "lookup_task",
                                        "arguments": '{"task_id":',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "content": "world",
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "42}"}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "choices": [],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        ]
        for payload in payloads:
            yield None, json.dumps(payload)
        yield None, "[DONE]"

    events = [event async for event in _translate_openai(frames())]

    assert events == [
        {"type": "text_delta", "text": "Hello "},
        {"type": "text_delta", "text": "world"},
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "lookup_task",
            "input": {"task_id": 42},
        },
        {"type": "usage", "input_tokens": 11, "output_tokens": 7},
        {
            "type": "done",
            "stop_reason": "tool_calls",
            "text": "Hello world",
            "session_id": None,
            "awaiting": None,
        },
    ]
