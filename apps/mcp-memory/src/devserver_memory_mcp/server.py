"""The ``DevServer`` MCP server — task control.

A small stdio server that lets any MCP-speaking coding CLI (Claude Code /
Codex / Gemini) drive DevServer end-to-end: create a task in a repo with a
chosen vendor/model, start it on the local worker, then read its live state
and final output. It owns no database — every tool is a thin ``httpx`` proxy
to the worker's ``/internal/tasks/*`` endpoints.

Design rules:
- **Provider-agnostic.** Identical tools for Claude Code, Codex, Gemini — no
  per-vendor branching. The calling CLI is tagged on created tasks via
  ``DEVSERVER_AGENT`` only for provenance.
- **Graceful degradation.** If the worker is unreachable or returns an error,
  tools return a short JSON ``{"error": "..."}`` so the model proceeds instead
  of crashing.
- **Local trust boundary.** stdio only — the server runs beside a coding CLI on
  the operator's machine, so the local pipe is the trust boundary.

Config (env):
- ``DEVSERVER_WORKER_URL``  — worker base URL (default ``http://localhost:8000``)
- ``DEVSERVER_TOKEN``       — optional bearer token for a remote worker
- ``DEVSERVER_AGENT``       — CLI name recorded in ``created_by`` on new tasks
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import httpx
from fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────
WORKER_URL = os.environ.get("DEVSERVER_WORKER_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("DEVSERVER_TOKEN", "").strip()
AGENT = os.environ.get("DEVSERVER_AGENT", "").strip()

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

# Server identity as clients see it (tool prefixes, `claude mcp list`).
mcp = FastMCP("DevServer")

# ── Internals ───────────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


class WorkerUnavailable(Exception):
    """Raised when the worker can't serve a request; carries a human-readable
    message that tools surface to the model verbatim."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=WORKER_URL, timeout=_TIMEOUT, headers=_HEADERS)
    return _client


def _git_toplevel() -> str:
    """Repo root of the console's cwd, or cwd itself when not in a git repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return os.getcwd()


async def _request(
    method: str, path: str, *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """Call the worker; return parsed JSON or raise ``WorkerUnavailable``."""
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = await _get_client().request(method, path, params=clean, json=json_body)
    except httpx.ConnectError:
        raise WorkerUnavailable(
            f"DevServer worker unreachable at {WORKER_URL} — start it with "
            "`uv run uvicorn src.main:app --port 8000` from apps/worker."
        )
    except httpx.HTTPError as exc:
        raise WorkerUnavailable(f"DevServer worker request failed: {exc}")

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text[:200]
        raise WorkerUnavailable(f"worker error {resp.status_code}: {detail}")
    try:
        return resp.json()
    except Exception:
        raise WorkerUnavailable("worker returned a non-JSON response")


async def _direct(method: str, path: str, *,
                  params: dict[str, Any] | None = None,
                  json_body: dict[str, Any] | None = None) -> str:
    """Call a worker path and JSON-encode, degrading gracefully."""
    try:
        return json.dumps(
            await _request(method, path, params=params, json_body=json_body),
            ensure_ascii=False,
        )
    except WorkerUnavailable as exc:
        return json.dumps({"error": str(exc)})


# ── Tools: task control (create / run / inspect DevServer tasks) ─────────────
# Enqueue is delegated by the worker to the Next.js queue producer (the single
# source of truth for the pgqueuer table).

@mcp.tool()
async def task_options() -> str:
    """List the valid agent vendors, models, task types and git flows for a new
    DevServer task. Call this before ``task_create`` to pick a supported model."""
    return await _direct("GET", "/internal/agent-registry")


@mcp.tool()
async def task_create(
    title: str,
    description: str = "",
    repo_id: int | None = None,
    task_key: str | None = None,
    task_type: str = "coding",
    agent_vendor: str = "anthropic",
    claude_model: str | None = None,
    claude_mode: str = "max",
    acceptance: str = "",
    priority: int = 3,
    git_flow: str = "branch",
    max_turns: int | None = None,
    skip_verify: bool = False,
    enqueue: bool = True,
) -> str:
    """Create a DevServer task and (by default) start it on the local worker.

    ``coding`` / ``test`` / ``script`` tasks need a repo: pass ``repo_id`` or
    leave it blank to target the repo of the current folder. ``skill`` /
    ``research`` tasks run without a repo. Pick ``agent_vendor`` /
    ``claude_model`` from ``task_options`` (``claude_mode`` is billing:
    ``max`` = subscription, ``api`` = API key). Returns
    ``{task_id, task_key, status, enqueued}`` — then poll ``task_status`` and
    read ``task_output`` for the result."""
    body: dict[str, Any] = {
        "title": title, "description": description, "acceptance": acceptance,
        "task_key": task_key, "task_type": task_type, "agent_vendor": agent_vendor,
        "claude_model": claude_model, "claude_mode": claude_mode, "priority": priority,
        "git_flow": git_flow, "max_turns": max_turns, "skip_verify": skip_verify,
        "enqueue": enqueue,
    }
    # Explicit repo_id wins; otherwise let the worker resolve this folder to a
    # DevServer repo so the task lands in the right codebase.
    if repo_id is not None:
        body["repo_id"] = repo_id
    else:
        body["repo_path"] = _git_toplevel()
    if AGENT:
        body["created_by"] = f"mcp:{AGENT}"
    return await _direct("POST", "/internal/tasks/create", json_body=body)


@mcp.tool()
async def task_run(task_key: str) -> str:
    """Start (or re-run) an existing DevServer task by key on the local worker."""
    return await _direct("POST", f"/internal/tasks/{task_key}/run")


@mcp.tool()
async def task_list(status: str | None = None, limit: int = 20) -> str:
    """List DevServer tasks (newest first), optionally filtered by status
    (pending/queued/running/verifying/done/failed/blocked/cancelled)."""
    return await _direct("GET", "/internal/tasks", params={"status": status, "limit": limit})


@mcp.tool()
async def task_status(task_key: str) -> str:
    """Current state of a DevServer task plus its latest run (status, pr_url,
    cost, turns, error)."""
    return await _direct("GET", f"/internal/tasks/{task_key}")


@mcp.tool()
async def task_output(task_key: str, lines: int = 120) -> str:
    """Tail a task's log — the agent's output / deliverable (the answer for
    research & skill tasks; progress + result for coding tasks)."""
    return await _direct("GET", f"/internal/tasks/{task_key}/log", params={"lines": lines})


@mcp.tool()
async def task_cancel(task_key: str) -> str:
    """Cancel a running, queued or pending DevServer task by key."""
    return await _direct("POST", f"/internal/tasks/{task_key}/cancel")


def main() -> None:
    """Console-script entry point (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
