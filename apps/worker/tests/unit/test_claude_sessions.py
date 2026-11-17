"""``services.claude_sessions``: delete safety, containment and redaction."""

import json
import os
import time

import pytest
from fastapi import HTTPException

from routes import sessions_night
from services import claude_sessions

SID = "f42f0824-1111-4222-8333-944444444444"
OLD = time.time() - 3600  # well outside LIVE_WINDOW_SECONDS


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    return home


def _transcript(home, slug="-work-repo", sid=SID, mtime=OLD):
    pdir = home / "projects" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{sid}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def test_delete_removes_transcripts_and_session_dirs(home):
    t1 = _transcript(home, "-a")
    t2 = _transcript(home, "-b")
    env_dir = home / "session-env" / SID
    hist_dir = home / "file-history" / SID
    for d in (env_dir, hist_dir):
        d.mkdir(parents=True)
        (d / "x").write_text("x")

    result = claude_sessions.delete_session(SID)

    assert result["deleted"] == 4
    assert result["errors"] == []
    assert result["freed_bytes"] > 0
    for p in (t1, t2, env_dir, hist_dir):
        assert not p.exists()


def test_delete_rejects_malformed_id(home):
    with pytest.raises(ValueError):
        claude_sessions.delete_session("../../etc/passwd")


def test_delete_missing_session_is_not_found(home):
    with pytest.raises(FileNotFoundError):
        claude_sessions.delete_session(SID)


def test_delete_refuses_recently_written_transcript(home):
    path = _transcript(home, mtime=time.time())
    with pytest.raises(claude_sessions.SessionIsLiveError):
        claude_sessions.delete_session(SID)
    assert path.exists()


def test_delete_refuses_registered_live_session(home):
    path = _transcript(home)
    reg = home / "sessions"
    reg.mkdir()
    (reg / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": SID}))
    with pytest.raises(claude_sessions.SessionIsLiveError):
        claude_sessions.delete_session(SID)
    assert path.exists()


def test_symlinked_project_dir_outside_home_is_never_touched(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / f"{SID}.jsonl"
    victim.write_text("precious\n")
    os.utime(victim, (OLD, OLD))
    (home / "projects" / "-evil").symlink_to(outside, target_is_directory=True)

    assert claude_sessions._transcript_paths(SID) == []
    with pytest.raises(FileNotFoundError):
        claude_sessions.delete_session(SID)
    assert victim.read_text() == "precious\n"


def test_symlinked_session_env_outside_home_is_not_removed(home, tmp_path):
    _transcript(home)
    outside = tmp_path / "outside-env"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (home / "session-env").mkdir()
    (home / "session-env" / SID).symlink_to(outside, target_is_directory=True)

    result = claude_sessions.delete_session(SID)

    assert result["deleted"] == 1  # only the transcript
    assert (outside / "keep").exists()


def test_delete_raises_when_nothing_could_be_removed(home, monkeypatch):
    _transcript(home)

    def boom(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(claude_sessions.Path, "unlink", boom)
    with pytest.raises(claude_sessions.SessionDeleteFailedError):
        claude_sessions.delete_session(SID)


async def test_route_refuses_delete_when_task_lookup_fails(home, monkeypatch):
    path = _transcript(home)

    async def down(_sid):
        raise ConnectionError("db down")

    monkeypatch.setattr(claude_sessions, "active_task_for_session", down)
    with pytest.raises(HTTPException) as exc:
        await sessions_night.claude_session_delete(SID)
    assert exc.value.status_code == 503
    assert path.exists()


async def test_route_refuses_delete_of_active_task_session(home, monkeypatch):
    path = _transcript(home)

    async def owner(_sid):
        return "DS-42"

    monkeypatch.setattr(claude_sessions, "active_task_for_session", owner)
    with pytest.raises(HTTPException) as exc:
        await sessions_night.claude_session_delete(SID)
    assert exc.value.status_code == 409
    assert "DS-42" in exc.value.detail
    assert path.exists()


async def test_route_returns_500_when_every_unlink_failed(home, monkeypatch):
    _transcript(home)

    async def none(_sid):
        return None

    def boom(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(claude_sessions, "active_task_for_session", none)
    monkeypatch.setattr(claude_sessions.Path, "unlink", boom)
    with pytest.raises(HTTPException) as exc:
        await sessions_night.claude_session_delete(SID)
    assert exc.value.status_code == 500


async def test_active_task_lookup_skips_db_for_malformed_id():
    assert await claude_sessions.active_task_for_session("not-a-uuid") is None


def test_clean_redacts_a_secret_straddling_the_cap():
    key = "ghp_" + "A" * 36
    text = "x" * 89 + " " + key + " tail"
    out, truncated, kinds = claude_sessions._clean(text, 100)
    assert truncated
    assert "ghp_" not in out
    assert kinds == ["github_pat"]


def test_clean_drops_a_secret_severed_by_the_pre_cap():
    cap = 100
    key = "ghp_" + "B" * 36
    # The key straddles cap + margin, where the bounded pre-cut slices it.
    pad = cap + claude_sessions._REDACT_MARGIN - 10
    text = "y" * pad + " " + key + " " + "z" * 5000
    out, truncated, _ = claude_sessions._clean(text, cap)
    assert truncated
    assert "BBBB" not in out


def test_notice_text_is_redacted():
    key = "ghp_" + "C" * 36
    events = claude_sessions._parse_line({"type": "ai-title", "aiTitle": f"fix {key}"})
    assert events and key not in events[0]["text"]
    events = claude_sessions._parse_line({"type": "mode", "mode": key})
    assert events and key not in events[0]["text"]
