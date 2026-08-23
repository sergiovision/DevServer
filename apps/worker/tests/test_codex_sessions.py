"""Tests for the read-only Codex session observatory."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from services.pro import codex_sessions


SESSION_ID = "019d9172-81c0-7800-8aa1-bbab2a035a53"


def _rollout(home: Path, events: list[dict]) -> Path:
    path = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "21"
        / f"rollout-2026-08-21T10-00-00-{SESSION_ID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _sample_events() -> list[dict]:
    return [
        {
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": SESSION_ID,
                "timestamp": "2026-08-21T10:00:00Z",
                "cwd": "/work/repository",
                "cli_version": "0.149.0",
                "originator": "codex_cli_rs",
                "git": {"branch": "feature/sessions"},
            },
        },
        {
            "timestamp": "2026-08-21T10:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        {
            "timestamp": "2026-08-21T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "private instructions"}],
            },
        },
        {
            "timestamp": "2026-08-21T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Add Codex sessions"}],
            },
        },
        {
            "timestamp": "2026-08-21T10:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Inspecting the viewer"}],
            },
        },
        {
            "timestamp": "2026-08-21T10:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "exec",
                "input": '{"command":"npm test"}',
            },
        },
        {
            "timestamp": "2026-08-21T10:00:06Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "all tests passed",
            },
        },
        {
            "timestamp": "2026-08-21T10:00:07Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Implemented."}],
            },
        },
        {
            "timestamp": "2026-08-21T10:00:08Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "cache_write_input_tokens": 5,
                        "output_tokens": 20,
                    }
                },
            },
        },
    ]


def test_list_codex_sessions_uses_rollout_metadata(tmp_path):
    home = tmp_path / ".codex"
    path = _rollout(home, _sample_events())
    os.utime(path, (1_700_000_000, 1_700_000_000))

    with patch.object(codex_sessions, "codex_home", return_value=home), patch.object(
        codex_sessions.time, "time", return_value=1_700_000_500
    ):
        rows = codex_sessions.list_sessions(limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == SESSION_ID
    assert row["label"] == "Add Codex sessions"
    assert row["cwd"] == "/work/repository"
    assert row["git_branch"] == "feature/sessions"
    assert row["version"] == "0.149.0"
    assert row["model"] == "gpt-5.6-sol"
    assert row["live"] is False
    assert row["status"] == "ended"


def test_tail_codex_session_normalizes_transcript_and_skips_developer_messages(tmp_path):
    home = tmp_path / ".codex"
    path = _rollout(home, _sample_events())

    with patch.object(codex_sessions, "codex_home", return_value=home):
        result = codex_sessions.tail_session(SESSION_ID)

    assert result["exists"] is True
    assert result["next_offset"] == path.stat().st_size
    assert [event["type"] for event in result["events"]] == [
        "text",
        "thinking",
        "tool_call",
        "tool_result",
        "text",
        "usage",
    ]
    assert [
        event.get("text")
        for event in result["events"]
        if event["type"] == "text"
    ] == ["Add Codex sessions", "Implemented."]
    assert result["events"][2]["summary"] == "npm test"
    assert result["events"][-1]["input_tokens"] == 100
    assert result["events"][-1]["cache_read_tokens"] == 80


def test_codex_session_id_cannot_escape_state_directory(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    with patch.object(codex_sessions, "codex_home", return_value=home):
        result = codex_sessions.tail_session("../../auth")

    assert result == {"events": [], "next_offset": 0, "size": 0, "exists": False}
