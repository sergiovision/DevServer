"""Task resource: create, start, list and inspect tasks.

Used by the dashboard, MCP clients and any HTTP caller. Starting a task
always goes through the Next.js producer (``services.queue_bridge``), the
single writer of the job queue.
"""

import os
import re
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, update

from models.base import async_session
from models.repo import Repo
from models.task import Task
from models.task_run import TaskRun
from routes.registry import _REPO_REQUIRED_TYPES, _VALID_TASK_TYPES
from services import (
    agent_backends,
    git_ops,
)
from services.queue_bridge import enqueue_via_web

router = APIRouter(prefix="/internal")


# ─── Models ─────────────────────────────────────────────────────────────────


class ModeRequest(BaseModel):
    mode: str  # "autonomous" or "interactive"


class TaskKeyRequest(BaseModel):
    task_key: str


class ContinueTaskRequest(BaseModel):
    model: str | None = None
    mode: str | None = None  # "max" or "api"


class CreateTaskRequest(BaseModel):
    """Body for POST /internal/tasks/create — mirrors the Next.js
    ``POST /api/tasks`` allow-list so an MCP client can create + start a task."""

    title: str
    description: str = ""
    acceptance: str = ""
    repo_id: int | None = None
    # A filesystem path (e.g. the MCP console's git toplevel) resolved to a
    # DevServer repo id when repo_id is omitted. Free-mode safe — resolved here
    # rather than via the Pro-only /repos/resolve endpoint.
    repo_path: str | None = None
    task_key: str | None = None  # auto-generated when blank
    task_type: str = "coding"
    agent_vendor: str = "anthropic"
    claude_model: str | None = None
    claude_mode: str = "max"  # billing: "max" | "api"
    priority: int = 3
    mode: str = "autonomous"  # "autonomous" | "interactive"
    git_flow: str = "branch"
    max_turns: int | None = None
    skip_verify: bool = False
    backup_vendor: str | None = None
    backup_model: str | None = None
    enqueue: bool = True  # create + immediately start
    created_by: str = "mcp"

    # NightCycleStartRequest moved to routes/pro_internal.py


def _task_summary(t: Task) -> dict:
    return {
        "id": t.id,
        "task_key": t.task_key,
        "title": t.title,
        "status": t.status,
        "task_type": t.task_type,
        "repo_id": t.repo_id,
        "priority": t.priority,
        "mode": t.mode,
        "agent_vendor": t.agent_vendor,
        "claude_model": t.claude_model,
        "claude_mode": t.claude_mode,
        "git_flow": t.git_flow,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _run_summary(r: TaskRun) -> dict:
    return {
        "attempt": r.attempt,
        "status": r.status,
        "branch": r.branch,
        "pr_url": r.pr_url,
        "cost_usd": float(r.cost_usd) if r.cost_usd is not None else 0.0,
        "turns": r.turns,
        "duration_ms": r.duration_ms,
        "error": ((r.error_log or "")[:2000] or None),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


def _repo_root_free(repo: Repo) -> str | None:
    """On-disk root for a repo (local folder or worktree). Free-mode copy of
    the Pro ``_repo_root`` used only for path→repo_id resolution."""
    if git_ops.is_local_provider(getattr(repo, "provider", None)):
        try:
            return git_ops.resolve_local_root(repo.gitea_url)
        except RuntimeError:
            return None
    return git_ops.get_worktree_path(repo.name)


def _path_under(requested: str, root: str | None) -> bool:
    if not root:
        return False
    try:
        req = os.path.realpath(requested)
        base = os.path.realpath(root)
    except Exception:
        return False
    return req == base or req.startswith(base + os.sep)


async def _resolve_repo_by_path(db, path: str) -> int | None:
    """Deepest active repo whose on-disk root contains ``path`` (or None)."""
    repos = (await db.execute(select(Repo).where(Repo.active == True))).scalars().all()  # noqa: E712
    best: tuple[int, int] | None = None  # (depth, repo_id)
    for repo in repos:
        root = _repo_root_free(repo)
        if _path_under(path, root):
            depth = len(os.path.realpath(root))
            if best is None or depth > best[0]:
                best = (depth, repo.id)
    return best[1] if best else None


# ─── Task control (MCP / external drivers) ──────────────────────────────────
# These free endpoints let an MCP client (or any HTTP caller) create, start,
# list, inspect and cancel tasks. Enqueue always goes through the Next.js
# producer (services/queue_bridge.enqueue_via_web) — the single source of
# truth for the pgqueuer table.


_STOPWORDS = {
    "the",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "for",
    "and",
    "or",
    "with",
    "make",
    "add",
    "ability",
    "its",
    "it",
    "into",
    "change",
    "use",
    "using",
    "should",
    "be",
    "is",
    "are",
    "this",
    "that",
    "from",
    "at",
    "as",
    "by",
}


def _slugify_task_key(text_value: str, *, max_words: int = 5) -> str:
    """Build a human-readable ``MCP-<SLUG>`` key from a title/description.

    Words come from the text (stop-words dropped) so the key describes the
    task instead of an opaque timestamp. Falls back to a short random token
    when the text yields nothing usable.
    """
    words = re.findall(r"[A-Za-z0-9]+", (text_value or "").lower())
    kept = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    if not kept:  # every word filtered out — keep the raw words
        kept = words
    slug = "-".join(kept[:max_words]).upper()
    if not slug:
        slug = secrets.token_hex(3).upper()
    return f"MCP-{slug}"


@router.post("/tasks/create")
async def create_task(req: CreateTaskRequest):
    """Create a task (status=pending) and optionally enqueue it immediately."""
    task_type = (req.task_type or "coding").strip()
    if task_type not in _VALID_TASK_TYPES:
        raise HTTPException(
            400, f"Invalid task_type '{task_type}' — one of {sorted(_VALID_TASK_TYPES)}"
        )
    vendor = (req.agent_vendor or "anthropic").strip()
    if vendor not in agent_backends.VENDOR_MODELS:
        raise HTTPException(
            400, f"Invalid agent_vendor '{vendor}' — one of {list(agent_backends.VENDOR_MODELS)}"
        )
    if req.backup_vendor and req.backup_vendor not in agent_backends.VENDOR_MODELS:
        raise HTTPException(400, f"Invalid backup_vendor '{req.backup_vendor}'")
    if req.claude_mode not in ("max", "api"):
        raise HTTPException(400, "claude_mode must be 'max' or 'api'")
    if req.max_turns is not None and req.max_turns <= 0:
        raise HTTPException(400, "max_turns must be a positive integer")
    if not (req.title or "").strip():
        raise HTTPException(400, "title is required")

    async with async_session() as db:
        repo_id = req.repo_id
        if repo_id is None and req.repo_path:
            repo_id = await _resolve_repo_by_path(db, req.repo_path)
        if task_type in _REPO_REQUIRED_TYPES and repo_id is None:
            raise HTTPException(
                400,
                f"repo_id (or a resolvable repo_path) is required for task_type '{task_type}'",
            )
        if repo_id is not None:
            repo = await db.get(Repo, repo_id)
            if not repo:
                raise HTTPException(404, f"Repo {repo_id} not found")

        task_key = (req.task_key or "").strip()
        if " " in task_key:
            raise HTTPException(400, "task_key must not contain spaces")

        async def _key_taken(k: str) -> bool:
            return (
                await db.execute(select(Task).where(Task.repo_id == repo_id, Task.task_key == k))
            ).scalar_one_or_none() is not None

        if task_key:
            # Explicit key supplied by the caller — must be unique.
            if await _key_taken(task_key):
                raise HTTPException(409, f"Task '{task_key}' already exists in this repo")
        else:
            # Auto-generate a descriptive MCP-<SLUG> key from the title
            # (falling back to the description), disambiguating collisions
            # with a numeric suffix rather than a timestamp.
            base = _slugify_task_key(req.title or req.description or "")
            task_key = base
            suffix = 2
            while await _key_taken(task_key):
                task_key = f"{base}-{suffix}"
                suffix += 1

        new_row = (
            await db.execute(
                text(
                    """
            INSERT INTO tasks
                (repo_id, task_key, title, description, acceptance, priority, mode,
                 task_type, claude_mode, agent_vendor, claude_model, max_turns,
                 skip_verify, git_flow, backup_vendor, backup_model, status, created_by)
            VALUES
                (:repo_id, :task_key, :title, :description, :acceptance, :priority, :mode,
                 :task_type, :claude_mode, :vendor, :model, :max_turns,
                 :skip_verify, :git_flow, :backup_vendor, :backup_model, 'pending', :created_by)
            RETURNING id
            """
                ),
                {
                    "repo_id": repo_id,
                    "task_key": task_key,
                    "title": req.title.strip()[:2000],
                    "description": req.description or "",
                    "acceptance": req.acceptance or "",
                    "priority": req.priority,
                    "mode": req.mode or "autonomous",
                    "task_type": task_type,
                    "claude_mode": req.claude_mode,
                    "vendor": vendor,
                    "model": req.claude_model,
                    "max_turns": req.max_turns,
                    "skip_verify": req.skip_verify,
                    "git_flow": req.git_flow or "branch",
                    "backup_vendor": req.backup_vendor,
                    "backup_model": req.backup_model,
                    "created_by": req.created_by or "mcp",
                },
            )
        ).fetchone()
        task_id = new_row[0]
        await db.commit()

    enqueued = False
    status = "pending"
    if req.enqueue:
        # enqueue_via_web hits Next.js, which flips status→queued + sets queue_job_id.
        enqueued = await enqueue_via_web(task_id)
        if enqueued:
            status = "queued"

    return {
        "task_id": task_id,
        "task_key": task_key,
        "repo_id": repo_id,
        "status": status,
        "enqueued": enqueued,
    }


@router.post("/tasks/{task_key}/run")
async def run_task_endpoint(task_key: str):
    """Enqueue/start an existing (non-active) task by key."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(404, f"Task {task_key} not found")
        if task.status in ("running", "verifying", "queued"):
            raise HTTPException(400, f"Task is {task.status} — already active")
        task_id = task.id
        # Normalise to pending so the Next.js enqueue guard (pending/failed/test)
        # accepts blocked/cancelled/done re-runs too.
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status="pending", updated_at=datetime.now(UTC))
        )
        await db.commit()

    enqueued = await enqueue_via_web(task_id)
    if not enqueued:
        raise HTTPException(502, "enqueue failed — is the Next.js web app running?")
    return {"task_key": task_key, "task_id": task_id, "status": "queued", "enqueued": True}


@router.get("/tasks")
async def list_tasks(
    status: str | None = None,
    repo_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
):
    """List tasks (newest first) with optional status / repo filters."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    async with async_session() as db:
        q = select(Task)
        if status:
            q = q.where(Task.status == status)
        if repo_id is not None:
            q = q.where(Task.repo_id == repo_id)
        q = q.order_by(Task.created_at.desc()).limit(limit).offset(offset)
        rows = (await db.execute(q)).scalars().all()
    return {"tasks": [_task_summary(t) for t in rows], "count": len(rows)}


@router.get("/tasks/{task_key}")
async def task_detail(task_key: str):
    """Full state of one task: the task row + its latest run (status, pr_url,
    cost, turns, error) so a caller gets state + final-output pointer at once."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(404, f"Task {task_key} not found")
        runs = (
            (
                await db.execute(
                    select(TaskRun)
                    .where(TaskRun.task_id == task.id)
                    .order_by(TaskRun.attempt.desc())
                )
            )
            .scalars()
            .all()
        )
    latest = runs[0] if runs else None
    return {
        **_task_summary(task),
        "description": task.description,
        "acceptance": task.acceptance,
        "attempts": len(runs),
        "latest_run": _run_summary(latest) if latest else None,
    }
