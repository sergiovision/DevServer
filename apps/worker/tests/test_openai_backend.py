"""Regression tests for the OpenAI Codex CLI integration."""

import json
from unittest.mock import patch

from services.agent_backends import OpenAIBackend


def _command(backend: OpenAIBackend) -> list[str]:
    return backend.build_command(
        prompt="Fix the focused issue and test it.",
        model="gpt-5.6-sol",
        allowed_tools="",
        session_id=None,
        max_turns=None,
    )


def test_codex_command_uses_supported_workspace_sandbox():
    backend = OpenAIBackend()
    with patch("services.agent_backends.settings.openai_base_url", ""), patch(
        "services.agent_backends.settings.openai_api_version", ""
    ):
        command = _command(backend)

    assert command[:2] == [backend.bin_argv0(), "exec"]
    assert command[-1] == "Fix the focused issue and test it."
    assert "--full-auto" not in command
    sandbox = command.index("--sandbox")
    assert command[sandbox + 1] == "workspace-write"


def test_codex_api_auth_bridges_openai_key_to_documented_cli_variable():
    backend = OpenAIBackend()
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True):
        env = backend.build_env("api")

    assert env is not None
    assert env["OPENAI_API_KEY"] == "test-key"
    assert env["CODEX_API_KEY"] == "test-key"


def test_codex_subscription_auth_strips_both_api_key_variables():
    backend = OpenAIBackend()
    with patch.dict(
        "os.environ",
        {"OPENAI_API_KEY": "openai-key", "CODEX_API_KEY": "codex-key", "KEEP": "yes"},
        clear=True,
    ):
        env = backend.build_env("max")

    assert env == {"KEEP": "yes"}


def test_codex_parser_handles_documented_jsonl_events():
    output = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "thread.started", "thread_id": "thread-123"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"id": "item-1", "type": "agent_message", "text": "Done."},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20},
            },
        ]
    )

    result = OpenAIBackend().parse_output(output, None)

    assert result.result == "Done."
    assert result.session_id == "thread-123"
    assert result.num_turns == 1
    assert result.errors == ["total_tokens=120"]


def test_codex_parser_surfaces_structured_turn_failure():
    output = json.dumps(
        {"type": "turn.failed", "error": {"message": "rate limit exceeded"}}
    )

    result = OpenAIBackend().parse_output(output, None)

    assert result.result == "rate limit exceeded"
    assert result.error == "rate limit exceeded"
