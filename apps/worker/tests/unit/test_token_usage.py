"""Specification: token usage reaches the ``cost_update`` event the analytics read."""

import json

from services.agent_backends import AgentResult, ClaudeBackend
from services.runner.attempt import _cost_update_payload


# Feature: F-ANL-05
def test_claude_usage_block_is_parsed_into_token_counts() -> None:
    raw = json.dumps(
        {
            "result": "ok",
            "usage": {
                "input_tokens": 120,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 300,
                "output_tokens": 800,
            },
        }
    )
    result = ClaudeBackend().parse_output(raw, None)
    assert (result.input_tokens, result.cache_read_tokens, result.cache_write_tokens) == (
        120,
        5000,
        300,
    )
    assert result.output_tokens == 800


# Feature: F-ANL-05
def test_malformed_usage_counts_as_zero() -> None:
    raw = json.dumps({"result": "ok", "usage": {"input_tokens": "lots", "output_tokens": -3}})
    result = ClaudeBackend().parse_output(raw, None)
    assert (result.input_tokens, result.output_tokens) == (0, 0)


# Feature: F-ANL-05
def test_cost_update_omits_token_keys_when_the_vendor_reported_none() -> None:
    payload = _cost_update_payload(
        AgentResult(num_turns=2), cost_usd=0.5, duration_ms=1000, vendor="openai"
    )
    assert "output_tokens" not in payload
    assert payload["vendor"] == "openai"


# Feature: F-ANL-05
def test_cost_update_carries_tokens_when_reported() -> None:
    result = AgentResult(input_tokens=10, cache_read_tokens=20, output_tokens=30)
    payload = _cost_update_payload(result, cost_usd=0.0, duration_ms=1000, vendor="anthropic")
    assert payload["output_tokens"] == 30
    assert payload["cache_read_tokens"] == 20
