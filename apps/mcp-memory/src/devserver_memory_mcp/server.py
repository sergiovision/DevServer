"""The ``DevServer`` MCP server — shared repo memory plus task control.

A small server that lets any MCP-speaking coding CLI (Claude Code / Codex /
Gemini) use DevServer's per-repo memory knowledge base (wake-up digest,
recall, facts, decisions, transcripts, code/doc corpus search) and drive
DevServer tasks end-to-end. It owns no database and no embedding model —
every tool is a thin ``httpx`` proxy to the worker's ``/internal`` endpoints
(``routes/memory_kb.py`` for memory, ``/internal/tasks/*`` for tasks).

Repo identity depends on the transport: over stdio it is resolved **once** from
the console's cwd (``git rev-parse --show-toplevel`` → ``/internal/repos/resolve``)
and cached for the process; over HTTP it comes from the calling peer's grant on
every request and is never cached (see ``remote.py``).

Design rules:
- **Provider-agnostic.** Identical tools for Claude Code, Codex, Gemini — no
  per-vendor branching. The calling CLI is tagged on created tasks via
  ``DEVSERVER_AGENT`` only for provenance.
- **Graceful degradation.** If the worker is unreachable or returns an error,
  tools return a short JSON ``{"error": "..."}`` so the model proceeds instead
  of crashing.
- **Pull-on-demand.** ``wake_digest`` once for grounding; pull a specific lane
  only when needed.

Transports:
- **stdio** (default) — runs beside a coding CLI on the operator's machine. The
  local pipe is the trust boundary: no authentication.
- **http** (``DEVSERVER_MCP_TRANSPORT=http``) — serves remote agents. Every
  request must present a DevServer A2A peer token and is scoped to that peer's
  repos; task tools route through the governed A2A gateway. See ``remote.py``.

Config (env):
- ``DEVSERVER_WORKER_URL``  — worker base URL (default ``http://localhost:8000``)
- ``DEVSERVER_REPO_ID``     — pin the repo id, skipping cwd resolution
- ``DEVSERVER_TOKEN``       — optional bearer token for a remote worker
- ``DEVSERVER_AGENT``       — CLI name recorded in ``created_by`` on new tasks
  and in ``metadata.agent`` on memory writes
- ``DEVSERVER_MCP_TRANSPORT`` / ``_HOST`` / ``_PORT`` — remote-mode transport
- ``DEVSERVER_WEB_URL``     — Next.js tier, for token introspection + task gateway
- ``DEVSERVER_ASSISTANT_SESSION`` — set by the Ask Agent panel's generated MCP
  config; binds ``propose_action`` to that chat conversation
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import httpx
from fastmcp import FastMCP

from . import remote
from .remote import ScopeError

# ── Config ────────────────────────────────────────────────────────────────
WORKER_URL = os.environ.get("DEVSERVER_WORKER_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("DEVSERVER_TOKEN", "").strip()
AGENT = os.environ.get("DEVSERVER_AGENT", "").strip()
_ENV_REPO_ID = os.environ.get("DEVSERVER_REPO_ID", "").strip()
ASSISTANT_SESSION = os.environ.get("DEVSERVER_ASSISTANT_SESSION", "").strip()

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

mcp = FastMCP(
    # Server identity as clients see it (tool prefixes, `claude mcp list`).
    "DevServer",
    # Remote mode requires a verified A2A peer token on every request; stdio
    # stays unauthenticated because the local pipe is already the boundary.
    auth=remote.DevServerTokenVerifier() if remote.is_remote() else None,
)

# ── Internals ───────────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None
_resolved_repo_id: int | None = None


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

    if resp.status_code == 403:
        raise WorkerUnavailable(
            "memory write disabled on the worker (memory_allow_console_writes=false)."
        )
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


async def _repo_id(explicit: int | None = None) -> int:
    """Resolve the DevServer repo id for the current request.

    Remote mode resolves from the *calling peer's* scope and caches nothing:
    the module-level cache below is right for stdio (one console, one repo,
    one process) but in a multi-tenant HTTP process it would hand one peer's
    repository memory to another.
    """
    if remote.is_remote():
        peer = remote.current_peer()
        assert peer is not None  # is_remote() implies an authenticated peer
        return remote.resolve_repo(explicit, peer)
    if explicit is not None:
        return explicit

    global _resolved_repo_id
    if _resolved_repo_id is not None:
        return _resolved_repo_id
    if _ENV_REPO_ID.isdigit():
        _resolved_repo_id = int(_ENV_REPO_ID)
        return _resolved_repo_id
    data = await _request("GET", "/internal/repos/resolve", params={"path": _git_toplevel()})
    rid = data.get("repo_id")
    if not isinstance(rid, int):
        raise WorkerUnavailable("worker did not return a repo_id for this folder.")
    _resolved_repo_id = rid
    return rid


async def _scoped(
    method: str, suffix: str, *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    repo_id: int | None = None,
) -> str:
    """Resolve the repo, call ``/internal/repos/{id}{suffix}``, JSON-encode."""
    try:
        rid = await _repo_id(repo_id)
        data = await _request(
            method, f"/internal/repos/{rid}{suffix}", params=params, json_body=json_body
        )
        return json.dumps(data, ensure_ascii=False)
    except ScopeError as exc:
        # An authorization failure, not an outage — say so plainly.
        return json.dumps({"error": f"not authorized: {exc}"})
    except WorkerUnavailable as exc:
        return json.dumps({"error": str(exc)})


def _with_agent(body: dict[str, Any]) -> dict[str, Any]:
    """Tag a memory write with its author for provenance.

    Remote writes are attributed to the peer slug, so a remote caller cannot
    forge authorship by setting DEVSERVER_AGENT on its own side.
    """
    if remote.is_remote():
        peer = remote.current_peer()
        if peer is not None:
            return {**body, "agent": f"a2a:{peer.slug}"}
    if AGENT:
        return {**body, "agent": AGENT}
    return body


async def _gateway(method: str, params: dict[str, Any] | None = None) -> str:
    """Call the A2A JSON-RPC gateway as the calling peer (remote mode only).

    Remote task tools go through here instead of the worker so a task created
    over MCP gets the same governance as one created over A2A: repo and
    task-type allow-lists, forced plan approval, patch-only git flow, spend
    ceilings and the daily quota.
    """
    try:
        result = await remote.a2a_rpc(remote.current_token(), method, params)
        return json.dumps(result, ensure_ascii=False)
    except ScopeError as exc:
        return json.dumps({"error": str(exc)})
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"A2A gateway unreachable: {exc}"})


# ── Tools: grounding ─────────────────────────────────────────────────────────


@mcp.tool()
async def wake_digest(repo_id: int | None = None) -> str:
    """**Call this first** in a session. A cheap (~300–500 token) digest that
    grounds you in this repo: identity, key facts, recent decisions. Read it
    before planning or editing."""
    return await _scoped("GET", "/wake-digest", repo_id=repo_id)


# ── Tools: memory read lanes (pull only what you need) ───────────────────────


@mcp.tool()
async def memory_recall(query: str, limit: int = 5, topic: str | None = None,
                        repo_id: int | None = None) -> str:
    """Hybrid recall of prior experience and distilled learnings for this repo
    ("what has been learned about X here?"). For past reasoning use
    memory_decisions; for established facts use memory_facts."""
    return await _scoped("GET", "/memory/recall",
                         params={"q": query, "limit": limit, "topic": topic},
                         repo_id=repo_id)


@mcp.tool()
async def memory_facts(query: str | None = None, subject: str | None = None,
                       limit: int = 10, repo_id: int | None = None) -> str:
    """Established project facts (build/test commands, where things live,
    invariants). Semantic search when ``query`` is given; otherwise the current
    facts, optionally filtered by ``subject``."""
    return await _scoped("GET", "/memory/facts",
                         params={"q": query, "subject": subject, "limit": limit},
                         repo_id=repo_id)


@mcp.tool()
async def memory_decisions(query: str, limit: int = 5, repo_id: int | None = None) -> str:
    """Past design decisions with their reasoning (problem → choice → why).
    Use for "why was X chosen / why not Y?"."""
    return await _scoped("GET", "/memory/decisions", params={"q": query, "limit": limit},
                         repo_id=repo_id)


@mcp.tool()
async def memory_transcripts(query: str, limit: int = 5, repo_id: int | None = None) -> str:
    """Search verbatim transcripts of past task runs — "did we already try this
    and fail?". **Check this before retrying a risky or expensive approach.**"""
    return await _scoped("GET", "/memory/transcripts", params={"q": query, "limit": limit},
                         repo_id=repo_id)


@mcp.tool()
async def repo_map(subdir: str | None = None, max_chars: int = 8000,
                   repo_id: int | None = None) -> str:
    """A symbol map (classes/functions/types) of the repo, or of ``subdir`` for
    a deeper view of the area you're editing. Pure local compute."""
    return await _scoped("GET", "/repo-map",
                         params={"subdir": subdir, "max_chars": max_chars},
                         repo_id=repo_id)


# ── Tools: corpus (semantic search over the repo's code & docs) ──────────────


@mcp.tool()
async def code_search(query: str, language: str | None = None,
                      path_prefix: str | None = None, limit: int = 8,
                      repo_id: int | None = None) -> str:
    """**Semantic** search over the repo's indexed source code — for conceptual
    questions where you don't know the symbol name. Filter by ``language``
    (e.g. "python", "typescript") or ``path_prefix``. For an exact symbol name,
    Grep is faster and always current."""
    return await _scoped("GET", "/corpus/search",
                         params={"q": query, "kind": "code", "language": language,
                                 "path_prefix": path_prefix, "limit": limit},
                         repo_id=repo_id)


@mcp.tool()
async def doc_search(query: str, limit: int = 8, repo_id: int | None = None) -> str:
    """Semantic search over the repo's indexed docs (design notes, READMEs, ADRs)."""
    return await _scoped("GET", "/corpus/search",
                         params={"q": query, "kind": "doc", "limit": limit},
                         repo_id=repo_id)


@mcp.tool()
async def corpus_stats(repo_id: int | None = None) -> str:
    """What's indexed for this repo: code/doc chunk + file counts and the last
    reindex time. Empty means the corpus hasn't been built yet."""
    return await _scoped("GET", "/corpus/stats", repo_id=repo_id)


@mcp.tool()
async def corpus_ingest(kind: str = "code", full: bool = False,
                        repo_id: int | None = None) -> str:
    """Re-index this repo's ``code`` or ``doc`` corpus now (in the background).
    The worker already re-indexes changed files after each successful task, so
    this is an escape hatch. ``full=true`` rebuilds from scratch."""
    return await _scoped("POST", "/corpus/ingest", json_body={"kind": kind, "full": full},
                         repo_id=repo_id)


@mcp.tool()
async def corpus_search_all(query: str, kind: str | None = None,
                            language: str | None = None, repo_ids: str | None = None,
                            limit: int = 8) -> str:
    """Semantic search across **every** DevServer repository at once, for when
    you don't know which repo holds the answer. ``kind`` is ``code`` or ``doc``
    (omit for both); ``repo_ids`` is an optional comma-separated allow-list.
    Each hit says which repo it came from."""
    if remote.is_remote():
        return json.dumps({"error": "corpus_search_all is not available to remote peers — "
                                    "use code_search / doc_search"})
    return await _direct("GET", "/internal/corpus/search",
                         params={"q": query, "kind": kind, "language": language,
                                 "repo_ids": repo_ids, "limit": limit})


# ── Tools: memory write lanes (enrich the shared knowledge base) ────────────


@mcp.tool()
async def memory_remember(content: str, topic: str | None = None,
                          repo_id: int | None = None) -> str:
    """Write a durable note (a gotcha, a convention) for future sessions and
    DevServer tasks in this repo."""
    return await _scoped("POST", "/memory/remember",
                         json_body=_with_agent({"content": content, "kind": "note",
                                                "topic": topic}),
                         repo_id=repo_id)


@mcp.tool()
async def record_fact(subject: str, predicate: str, object: str,
                      topic: str | None = None, repo_id: int | None = None) -> str:
    """Record (or supersede) a fact as subject-predicate-object, e.g.
    ("build", "command", "npm run build"). A new fact for the same
    subject+predicate replaces the old one."""
    return await _scoped("POST", "/facts",
                         json_body=_with_agent({"subject": subject, "predicate": predicate,
                                                "object": object, "topic": topic}),
                         repo_id=repo_id)


@mcp.tool()
async def record_decision(problem: str, choice: str, alternatives: list[str] | None = None,
                          reasoning: str = "", outcome: str = "",
                          repo_id: int | None = None) -> str:
    """Record a design decision so future sessions recall the reasoning: the
    ``problem``, the ``choice``, the ``alternatives`` considered, the
    ``reasoning`` and (if known) the ``outcome``."""
    return await _scoped("POST", "/decisions",
                         json_body=_with_agent({"problem": problem, "choice": choice,
                                                "alternatives": alternatives or [],
                                                "reasoning": reasoning, "outcome": outcome}),
                         repo_id=repo_id)


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
    if remote.is_remote():
        # Vendor/model/billing/turns/skip_verify are deliberately dropped: the
        # peer's credential decides those, and honouring them here would let a
        # remote caller opt out of the operator's policy.
        meta: dict[str, Any] = {"taskType": task_type, "title": title}
        if acceptance:
            meta["acceptance"] = acceptance
        if repo_id is not None:
            meta["repoId"] = repo_id
        return await _gateway("message/send", {
            "message": {
                "role": "user",
                "messageId": f"mcp-{task_key or title[:32]}",
                "parts": [{"kind": "text", "text": description or title}],
                "metadata": meta,
            },
        })
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
    if remote.is_remote():
        # No A2A equivalent, and re-running a task would sidestep the peer's
        # daily quota. Remote peers create a new task instead.
        return json.dumps({"error": "task_run is not available to remote peers — "
                                    "use task_create to submit new work"})
    return await _direct("POST", f"/internal/tasks/{task_key}/run")


@mcp.tool()
async def task_list(status: str | None = None, limit: int = 20) -> str:
    """List DevServer tasks (newest first), optionally filtered by status
    (pending/queued/running/verifying/done/failed/blocked/cancelled)."""
    if remote.is_remote():
        return await _gateway("tasks/list", {"limit": limit})
    return await _direct("GET", "/internal/tasks", params={"status": status, "limit": limit})


@mcp.tool()
async def task_status(task_key: str) -> str:
    """Current state of a DevServer task plus its latest run (status, pr_url,
    cost, turns, error)."""
    if remote.is_remote():
        # Remote peers address tasks by the opaque id the gateway returned.
        return await _gateway("tasks/get", {"id": task_key})
    return await _direct("GET", f"/internal/tasks/{task_key}")


@mcp.tool()
async def task_output(task_key: str, lines: int = 120) -> str:
    """Tail a task's log — the agent's output / deliverable (the answer for
    research & skill tasks; progress + result for coding tasks)."""
    if remote.is_remote():
        # The log file is a host artefact; the gateway returns the same
        # deliverable as a redacted output artifact.
        return await _gateway("tasks/get", {"id": task_key})
    return await _direct("GET", f"/internal/tasks/{task_key}/log", params={"lines": lines})


@mcp.tool()
async def task_cancel(task_key: str) -> str:
    """Cancel a running, queued or pending DevServer task by key."""
    if remote.is_remote():
        return await _gateway("tasks/cancel", {"id": task_key})
    return await _direct("POST", f"/internal/tasks/{task_key}/cancel")


# ── Tools: Ask Agent action proposals ────────────────────────────────────────


@mcp.tool()
async def propose_action(
    kind: str,
    summary: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Propose a side-effecting action for HUMAN APPROVAL.

    **This does NOT perform the action.** It shows the operator a confirmation
    card. After calling it you must STOP and end your turn — you will be told
    the outcome and resumed. Do not attempt the action another way, and do not
    propose more than one at a time.

    kind:
      - ``run_script``   — params ``{"script": "<key>"}``. Allowed keys:
        ``dbrestart`` (restart PostgreSQL and verify it came back),
        ``migrate`` (apply database migrations), ``restart`` (restart the
        DevServer processes), ``backup`` (back up DB + settings + .env).
        You supply the key only — never a path, never arguments.
      - ``task_create`` — params ``{"title", "description", "repo_id"?,
        "task_type"?, "enqueue"?}``.

    Only propose what the operator actually asked for.
    """
    if remote.is_remote():
        # Proposals land in the local operator's Ask Agent panel and can lead
        # to scripts running on this host — never something a remote peer
        # may queue up, even pending approval.
        return json.dumps({"error": "propose_action is not available to remote peers"})
    body: dict[str, Any] = {
        "kind": kind,
        "summary": summary,
        "params": params or {},
    }
    if ASSISTANT_SESSION:
        body["session_id"] = ASSISTANT_SESSION
    return await _direct("POST", "/internal/assistant/actions", json_body=body)


def main() -> None:
    """Console-script entry point.

    stdio by default. ``DEVSERVER_MCP_TRANSPORT=http`` serves remote agents
    over Streamable HTTP, bound to ``127.0.0.1`` unless the operator chooses
    otherwise: this server proxies the unauthenticated worker, so it must never
    be exposed by accident.
    """
    if remote.is_remote():
        mcp.run(transport="http", host=remote.HOST, port=remote.PORT)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
