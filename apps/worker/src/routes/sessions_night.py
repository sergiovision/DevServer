"""Night cycle controls and the Claude Code / Codex session observatory.

* ``/night-cycle/*`` — start, stop and inspect the overnight batch runner
  (``services/night_cycle.py``).
* ``/claude-sessions`` and ``/codex-sessions`` — read-only view of every agent
  CLI session on this host (``services/claude_sessions.py`` and
  ``services/codex_sessions.py``); Claude sessions can also be deleted.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.base import async_session
from services import claude_sessions, codex_sessions, night_cycle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal")


# ─── Night cycle ─────────────────────────────────────────────────────────────


class NightCycleStartRequest(BaseModel):
    end_hour: int = 7
    #: Opt-in: restart the worker before retrying a failed task. Kills every
    #: in-flight task and is a no-op when the worker runs in its own container.
    restart_on_failed: bool = False


@router.post("/night-cycle/start")
async def start_night_cycle(req: NightCycleStartRequest):
    """Start the night cycle with a given end hour (UTC)."""
    if req.end_hour < 0 or req.end_hour > 23:
        raise HTTPException(status_code=400, detail="end_hour must be 0–23")
    result = await night_cycle.start(end_hour=req.end_hour, restart_on_failed=req.restart_on_failed)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/night-cycle/stop")
async def stop_night_cycle():
    """Stop the night cycle."""
    return await night_cycle.stop()


@router.get("/night-cycle/status")
async def night_cycle_status():
    """Return current night cycle state."""
    status = await night_cycle.get_status()
    if status is None:
        return {"active": False}
    return status


# ─── Claude session observatory ──────────────────────────────────────────────
# See services/claude_sessions.py for the data sources and the Docker note.
# The scans, transcript reads (up to 2 MB) and rmtree are blocking filesystem
# work, so they run in a thread rather than on the event loop.


@router.get("/claude-sessions")
async def claude_sessions_list(limit: int = 60, live_only: bool = False):
    """List Claude sessions on this host, most recently active first."""
    try:
        rows = await asyncio.to_thread(
            claude_sessions.list_sessions,
            limit=limit,
            live_only=live_only,
        )
    except Exception as exc:
        raise HTTPException(500, f"could not scan Claude sessions: {exc}") from exc

    async with async_session() as db:
        rows = await claude_sessions.enrich_sessions(db, rows)

    return {
        "sessions": rows,
        "claude_home": str(claude_sessions.claude_home()),
        "home_exists": claude_sessions.claude_home().is_dir(),
    }


@router.get("/claude-sessions/{session_id}/tail")
async def claude_session_tail(session_id: str, since: int = 0, max_events: int = 3000):
    """Tail one session's transcript from a byte offset, as parsed events."""
    result = await asyncio.to_thread(
        claude_sessions.tail_session,
        session_id,
        since=since,
        max_events=max_events,
    )
    if not result.get("exists"):
        raise HTTPException(404, f"no transcript for session {session_id!r}")
    return result


@router.delete("/claude-sessions/{session_id}")
async def claude_session_delete(session_id: str):
    """Erase a session from disk. Irreversible — there is no trash."""
    try:
        # A DevServer task still driving this session (headless agents never
        # register as live) blocks the delete. If that cannot be checked,
        # refuse rather than risk erasing a running agent's transcript.
        try:
            owner = await claude_sessions.active_task_for_session(session_id)
        except Exception as exc:
            logger.warning("claude session delete: task lookup failed: %s", exc)
            raise HTTPException(
                503,
                "could not verify that no DevServer task is using this "
                "session (database unavailable); nothing was deleted",
            ) from exc
        if owner:
            raise claude_sessions.SessionIsLiveError(
                f"session belongs to task {owner}, which is still in progress"
            )
        return await asyncio.to_thread(claude_sessions.delete_session, session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except claude_sessions.SessionIsLiveError as exc:
        # 409: the request is valid, the resource is just in the wrong state.
        raise HTTPException(409, str(exc)) from exc
    except claude_sessions.SessionDeleteFailedError as exc:
        raise HTTPException(500, str(exc)) from exc


# ─── OpenAI Codex session observatory (read-only) ────────────────────────────


@router.get("/codex-sessions")
async def codex_sessions_list(limit: int = 60, live_only: bool = False):
    """List Codex CLI rollout sessions on this host."""
    try:
        rows = await asyncio.to_thread(
            codex_sessions.list_sessions,
            limit=limit,
            live_only=live_only,
        )
    except Exception as exc:
        raise HTTPException(500, f"could not scan Codex sessions: {exc}") from exc

    # Reuse the vendor-neutral task/repository attribution logic.
    async with async_session() as db:
        rows = await claude_sessions.enrich_sessions(db, rows)

    return {
        "sessions": rows,
        "codex_home": str(codex_sessions.codex_home()),
        "home_exists": (codex_sessions.codex_home() / "sessions").is_dir(),
    }


@router.get("/codex-sessions/{session_id}/tail")
async def codex_session_tail(session_id: str, since: int = 0, max_events: int = 3000):
    """Tail one Codex rollout from a byte offset, as normalized events."""
    result = await asyncio.to_thread(
        codex_sessions.tail_session,
        session_id,
        since=since,
        max_events=max_events,
    )
    if not result.get("exists"):
        raise HTTPException(404, f"no Codex rollout for session {session_id!r}")
    return result
