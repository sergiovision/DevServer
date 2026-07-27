"""Internal management API — called by FinCore TelegramController.

All endpoints are prefixed /internal and expect an X-Internal-Token header
matching INTERNAL_API_TOKEN env var (optional but recommended in production).
"""

import json as _json
import os
import re
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, text, update

from config import settings
from models.base import async_session
from models.repo import Repo
from models.setting import Setting
from models.task import Task
from models.task_run import TaskRun
from services.queue_bridge import enqueue_via_web
from services.queue_consumer import is_consumer_running
from services import agent_backends
from services import compaction
from services import decomposer
from services import git_ops
from services import llm_client
from services import repo_map
from services import scheduler
from services import side_effect_gate
from services import skills as skills_svc

router = APIRouter(prefix="/internal")


def _extract_json_object(text_content: str) -> dict:
    """Best-effort parse of an LLM response into a JSON object.

    Models — Gemini especially, with its "thinking" output — sometimes wrap the
    JSON in markdown fences or add a prose preamble. Try a direct parse after
    stripping fences; on failure, fall back to slicing the outermost ``{...}``
    span. Raises ``ValueError`` (with a snippet of the raw text) when nothing
    parses, so the caller can surface a useful diagnostic instead of a blank
    "could not parse" 500.
    """
    cleaned = (text_content or "").replace("```json", "").replace("```", "").strip()
    if cleaned:
        try:
            return _json.loads(cleaned)
        except _json.JSONDecodeError:
            pass
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return _json.loads(cleaned[start:end + 1])
            except _json.JSONDecodeError:
                pass
    snippet = (text_content or "").strip()[:300] or "(empty response)"
    raise ValueError(f"response was not valid JSON: {snippet}")


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


_VALID_TASK_TYPES = {"coding", "test", "skill", "script", "research"}
_REPO_REQUIRED_TYPES = {"coding", "test", "script"}


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


# ─── Status ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def worker_status():
    """Full devserver status: mode, paused, queue stats, active tasks."""
    worker_running = is_consumer_running()

    async with async_session() as db:
        # Active tasks (running or verifying)
        res = await db.execute(
            select(Task).where(Task.status.in_(["running", "verifying"]))
        )
        active = res.scalars().all()

        # Pending/queued tasks
        res2 = await db.execute(
            select(Task)
            .where(Task.status.in_(["pending", "queued"]))
            .order_by(Task.priority)
        )
        queued = res2.scalars().all()

        # Settings
        res3 = await db.execute(select(Setting))
        settings_rows = {s.key: s.value for s in res3.scalars().all()}

    mode = settings_rows.get("mode", "autonomous")
    if isinstance(mode, str) and mode.startswith('"'):
        import json
        mode = json.loads(mode)
    paused = settings_rows.get("paused", False)
    if isinstance(paused, str):
        import json
        paused = json.loads(paused)

    def fmt_priority(p: int) -> str:
        return {1: "critical", 2: "high", 3: "medium", 4: "low"}.get(p, str(p))

    return {
        "worker_running": worker_running,
        "mode": mode,
        "paused": paused,
        "active_tasks": [
            {
                "id": t.id,
                "task_key": t.task_key,
                "title": t.title,
                "status": t.status,
                "priority": fmt_priority(t.priority),
            }
            for t in active
        ],
        "queued_tasks": [
            {
                "id": t.id,
                "task_key": t.task_key,
                "title": t.title,
                "status": t.status,
                "priority": fmt_priority(t.priority),
            }
            for t in queued
        ],
        "counts": {
            "active": len(active),
            "queued": len(queued),
        },
    }


# ─── Queue control ───────────────────────────────────────────────────────────

@router.post("/pause")
async def pause_queue():
    """Pause task dispatching."""
    async with async_session() as db:
        setting = await db.get(Setting, "paused")
        if setting:
            setting.value = True
            setting.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key="paused", value=True))
        await db.commit()
    return {"paused": True, "message": "\u23f8 Dispatching paused"}


@router.post("/resume")
async def resume_queue():
    """Resume task dispatching."""
    async with async_session() as db:
        setting = await db.get(Setting, "paused")
        if setting:
            setting.value = False
            setting.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key="paused", value=False))
        await db.commit()
    return {"paused": False, "message": "\u25b6\ufe0f Dispatching resumed"}


@router.post("/mode")
async def set_mode(req: ModeRequest):
    """Set execution mode: autonomous or interactive."""
    mode = req.mode.lower()
    if mode == "auto":
        mode = "autonomous"
    if mode not in ("autonomous", "interactive"):
        raise HTTPException(status_code=400, detail="mode must be 'autonomous' or 'interactive'")

    async with async_session() as db:
        setting = await db.get(Setting, "mode")
        if setting:
            setting.value = mode
            setting.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key="mode", value=mode))
        await db.commit()

    return {"mode": mode, "message": f"Mode set to: {mode}"}


# ─── Task commands ───────────────────────────────────────────────────────────

@router.post("/tasks/{task_key}/approve")
async def approve_task(task_key: str):
    """Approve a pending task — set status to queued."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")
        if task.status not in ("pending", "blocked"):
            raise HTTPException(status_code=400, detail=f"Task is {task.status}, cannot approve")

        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="queued", updated_at=datetime.now(timezone.utc))
        )
        await db.commit()

    return {"task_key": task_key, "status": "queued", "message": f"\u2705 Approved {task_key}"}


@router.post("/tasks/{task_key}/reject")
async def reject_task(task_key: str):
    """Reject a pending task."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")

        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="cancelled", updated_at=datetime.now(timezone.utc))
        )
        await db.commit()

    return {"task_key": task_key, "status": "cancelled", "message": f"\U0001f6ab Rejected {task_key}"}


@router.post("/tasks/{task_key}/retry")
async def retry_task(task_key: str):
    """Re-queue a failed task."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")
        if task.status not in ("failed", "cancelled"):
            raise HTTPException(status_code=400, detail=f"Task is {task.status}, can only retry failed/cancelled")

        await db.execute(
            update(Task)
            .where(Task.task_key == task_key)
            .values(status="queued", updated_at=datetime.now(timezone.utc))
        )
        await db.commit()

    return {"task_key": task_key, "status": "queued", "message": f"\U0001f504 Re-queued {task_key}"}


@router.post("/cancel/{task_id}")
async def cancel_task(task_id: int):
    """Cancel a running or pending task by DB id."""
    async with async_session() as db:
        task = await db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status in ("test", "cancelled", "retired"):
            raise HTTPException(status_code=400, detail=f"Task is already {task.status}")

        old_status = task.status
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status="cancelled", updated_at=datetime.now(timezone.utc))
        )
        await db.execute(
            update(TaskRun)
            .where(TaskRun.task_id == task_id)
            .where(TaskRun.status.in_(["started", "verifying"]))
            .values(
                status="failed",
                finished_at=datetime.now(timezone.utc),
                error_log="Cancelled by user",
            )
        )
        await db.commit()

    return {"task_id": task_id, "old_status": old_status, "new_status": "cancelled"}


# ─── Task control (MCP / external drivers) ──────────────────────────────────
# These free endpoints let an MCP client (or any HTTP caller) create, start,
# list, inspect and cancel tasks. Enqueue always goes through the Next.js
# producer (services/queue_bridge.enqueue_via_web) — the single source of
# truth for the pgqueuer table.

@router.get("/agent-registry")
async def agent_registry():
    """Valid vendors / models / task types so a caller can build a task."""
    return {
        "vendors": agent_backends.VENDOR_MODELS,
        "vendor_labels": agent_backends.VENDOR_LABELS,
        "default_vendor": agent_backends.DEFAULT_VENDOR,
        "task_types": sorted(_VALID_TASK_TYPES),
        "repo_required_types": sorted(_REPO_REQUIRED_TYPES),
        "git_flows": ["branch", "commit", "patch", "untracked"],
        "billing_modes": ["max", "api"],
        "modes": ["autonomous", "interactive"],
    }


_STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "with",
    "make", "add", "ability", "its", "it", "into", "change", "use", "using",
    "should", "be", "is", "are", "this", "that", "from", "at", "as", "by",
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
        raise HTTPException(400, f"Invalid task_type '{task_type}' — one of {sorted(_VALID_TASK_TYPES)}")
    vendor = (req.agent_vendor or "anthropic").strip()
    if vendor not in agent_backends.VENDOR_MODELS:
        raise HTTPException(400, f"Invalid agent_vendor '{vendor}' — one of {list(agent_backends.VENDOR_MODELS)}")
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
            return (await db.execute(
                select(Task).where(Task.repo_id == repo_id, Task.task_key == k)
            )).scalar_one_or_none() is not None

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

        new_row = (await db.execute(text(
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
        ), {
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
        })).fetchone()
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
            update(Task).where(Task.id == task_id)
            .values(status="pending", updated_at=datetime.now(timezone.utc))
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
        runs = (await db.execute(
            select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.attempt.desc())
        )).scalars().all()
    latest = runs[0] if runs else None
    return {
        **_task_summary(task),
        "description": task.description,
        "acceptance": task.acceptance,
        "attempts": len(runs),
        "latest_run": _run_summary(latest) if latest else None,
    }


@router.post("/tasks/{task_key}/cancel")
async def cancel_task_by_key(task_key: str):
    """Cancel a task by key — works on running/verifying/queued/pending."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(404, f"Task {task_key} not found")
        if task.status in ("cancelled", "done", "retired"):
            raise HTTPException(400, f"Task is already {task.status}")
        task_id = task.id
        old_status = task.status
        now = datetime.now(timezone.utc)
        await db.execute(
            update(Task).where(Task.id == task_id).values(status="cancelled", updated_at=now)
        )
        await db.execute(
            update(TaskRun)
            .where(TaskRun.task_id == task_id)
            .where(TaskRun.status.in_(["started", "verifying"]))
            .values(status="failed", finished_at=now, error_log="Cancelled by user")
        )
        await db.commit()
    return {"task_key": task_key, "task_id": task_id, "old_status": old_status, "new_status": "cancelled"}


# ─── Refresh Git ───────────────────────────────────────────────────────────

@router.post("/repos/{repo_id}/refresh-git")
async def refresh_git(repo_id: int):
    """Clone or fetch a repo's bare repo and worktree."""
    async with async_session() as db:
        repo = await db.get(Repo, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")

    if git_ops.is_local_provider(getattr(repo, "provider", None)):
        # Local-folder repo — nothing to clone or fetch, just validate the
        # Local Root Folder (stored in repos.gitea_url) is a git checkout.
        result = await git_ops.refresh_local_repo(repo.gitea_url)
    else:
        result = await git_ops.refresh_repo(
            repo_name=repo.name,
            clone_url=repo.clone_url,
            default_branch=repo.default_branch,
            gitea_token=repo.gitea_token or None,
            provider=getattr(repo, "provider", None),
        )
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.get("/repos/{repo_id}/diagram")
async def repo_diagram(repo_id: int):
    """Mermaid architecture diagram (module tree) for a repo.

    Deterministic, no LLM — built on the free ``repo_map`` module. Resolves the
    repo's on-disk root (the Local Root Folder for local repos, else the task
    worktree) and walks the top directory levels.
    """
    async with async_session() as db:
        repo = await db.get(Repo, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")

    if git_ops.is_local_provider(getattr(repo, "provider", None)):
        try:
            root = git_ops.resolve_local_root(repo.gitea_url)
        except RuntimeError:
            root = None
    else:
        root = git_ops.get_worktree_path(repo.name)

    if not root or not os.path.isdir(root):
        raise HTTPException(
            status_code=409,
            detail="repo worktree not available — run Refresh Git first",
        )

    mermaid, stats = repo_map.build_mermaid(root)
    return {"repo_id": repo_id, "mermaid": mermaid, "stats": stats}


# ─── Task continuation ──────────────────────────────────────────────────────

@router.post("/tasks/{task_key}/continue")
async def continue_task(task_key: str, req: ContinueTaskRequest):
    """Prepare a task for continuation with an optional model/mode switch.

    If the task is currently running, in-flight runs are marked failed
    (same as cancel) but git state and session are preserved.  The repo
    lock is released so the re-enqueued job can acquire it immediately.
    The caller (web API) is expected to re-enqueue the task after this
    returns.
    """
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_key} not found")
        # ``test`` is a post-verify "waiting for human QA" state that still
        # has a live agent branch + session_id, so operators can drop a
        # follow-up message into the inbox and continue the same task
        # without reopening (which would reset the session).
        if task.status in ("done", "retired"):
            raise HTTPException(
                status_code=400,
                detail=f"Task is {task.status}, cannot continue",
            )

        old_status = task.status

        # If running, mark in-flight runs as failed (preserves session_id).
        if old_status in ("running", "verifying"):
            await db.execute(
                update(TaskRun)
                .where(TaskRun.task_id == task.id)
                .where(TaskRun.status.in_(["started", "verifying"]))
                .values(
                    status="failed",
                    finished_at=datetime.now(timezone.utc),
                    error_log="Interrupted for continuation",
                )
            )

        # Release the repo lock held by the current run so that the
        # re-enqueued job can acquire it immediately. The old run_task
        # finally-block will attempt to release the same lock later but
        # that is a harmless no-op (DELETE … WHERE task_key = :key).
        # Repo-less tasks (skill/research) have no lock to release — guard the
        # lookup so db.get is never called with a NULL primary key.
        repo = await db.get(Repo, task.repo_id) if task.repo_id else None
        if repo:
            await db.execute(text(
                "DELETE FROM repo_locks WHERE repo_name = :repo_name"
            ), {"repo_name": repo.name})

        # Apply optional model/mode overrides.
        updates: dict = {
            "is_continuation": True,
            "status": "pending",
            "updated_at": datetime.now(timezone.utc),
        }
        if req.model is not None:
            updates["claude_model"] = req.model or None
        if req.mode is not None and req.mode in ("max", "api"):
            updates["claude_mode"] = req.mode

        await db.execute(
            update(Task).where(Task.id == task.id).values(**updates)
        )
        await db.commit()

    return {
        "task_key": task_key,
        "old_status": old_status,
        "new_status": "pending",
        "model": req.model,
        "mode": req.mode,
    }


# ─── Task Log ────────────────────────────────────────────────────────────────

@router.get("/tasks/{task_key}/log")
async def task_log_tail(task_key: str, lines: int = 50):
    """Return last N lines from a task's log file."""
    log_path = os.path.join(settings.log_dir, f"{task_key}.log")
    if not os.path.exists(log_path):
        return {"lines": []}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=lines)
        return {"lines": list(tail)}
    except Exception:
        return {"lines": []}


# Outcome predictions are pre-run forecasts over historical data, so a short
# TTL cache is always safe. It matters in Pro: the similar-task predictor
# embeds the task text on CPU (fastembed) per call, which is too slow to
# re-run on every visit to the same task page.
_PREDICTION_CACHE: dict[str, tuple[float, dict | None]] = {}
_PREDICTION_TTL_SECONDS = 300.0
_PREDICTION_CACHE_MAX = 256


@router.get("/tasks/{task_key}/prediction")
async def task_prediction(task_key: str):
    """Forecast a task's outcome (migration 010).

    Free tier returns a repo-level baseline from ``task_runs``. The Pro
    edition first tries the embeddings-based similar-task predictor and only
    falls back to the baseline when there's no similar-task signal. One
    endpoint, both editions — the response carries a ``basis`` field
    (``similar`` vs ``repo``) so the UI can label the source.
    """
    from services import outcome

    hit = _PREDICTION_CACHE.get(task_key)
    if hit is not None and time.monotonic() - hit[0] < _PREDICTION_TTL_SECONDS:
        return {"prediction": hit[1]}

    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if task is None:
            raise HTTPException(404, f"task {task_key!r} not found")

        pred = None
        try:  # Pro: similarity-based forecast (absent in free → ImportError)
            from services.pro import hooks as pro
            pred = await pro.repo_memory(db, task.repo_id).predict_outcome(
                task.title, task.description or "",
            )
        except ImportError:
            pred = None

        # Fall back to the repo-level baseline when Pro is absent or had no
        # similar-task signal to work with.
        if not pred or not pred.get("sample_size"):
            pred = await outcome.predict_outcome_basic(db, task.repo_id)

    if len(_PREDICTION_CACHE) >= _PREDICTION_CACHE_MAX:
        oldest = min(_PREDICTION_CACHE, key=lambda k: _PREDICTION_CACHE[k][0])
        _PREDICTION_CACHE.pop(oldest, None)
    _PREDICTION_CACHE[task_key] = (time.monotonic(), pred)
    return {"prediction": pred}


# ─── System LLM settings ────────────────────────────────────────────────────

async def _read_system_llm_settings() -> tuple[str, str, str]:
    """Return (vendor, model, mode) for the system LLM from the settings table.

    Settings store values as JSON strings — strip the outer quotes. Defaults
    to GLM-5.1 and ``mode='max'`` (subscription) when a key is unset. ``mode``
    is the billing/transport path forwarded to ``llm_client.complete``
    (``'max'`` = subscription via vendor CLI, ``'api'`` = direct HTTP).
    """
    async with async_session() as db:
        vendor_row = await db.execute(
            select(Setting).where(Setting.key == "system_llm_vendor")
        )
        model_row = await db.execute(
            select(Setting).where(Setting.key == "system_llm_model")
        )
        mode_row = await db.execute(
            select(Setting).where(Setting.key == "system_llm_mode")
        )
        vendor_setting = vendor_row.scalar_one_or_none()
        model_setting = model_row.scalar_one_or_none()
        mode_setting = mode_row.scalar_one_or_none()

    sys_vendor = "glm"
    sys_model = "glm-5.1"
    sys_mode = "max"
    if vendor_setting and vendor_setting.value:
        v = vendor_setting.value
        sys_vendor = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)
    if model_setting and model_setting.value:
        v = model_setting.value
        sys_model = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)
    if mode_setting and mode_setting.value:
        v = mode_setting.value
        sys_mode = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)
    return sys_vendor, sys_model, sys_mode


# ─── DevTask Skill ──────────────────────────────────────────────────────────

class GenerateTaskRequest(BaseModel):
    description: str


@router.post("/generate-task")
async def generate_task(body: GenerateTaskRequest):
    """Call the system LLM with devtask skill prompt to generate task JSON.

    Uses whichever vendor/model is configured in the ``system_llm_vendor``
    and ``system_llm_model`` settings (editable on the /settings page).
    Defaults to GLM-5.1 if the settings haven't been created yet.
    """
    # Read system LLM vendor/model/mode from settings table
    sys_vendor, sys_model, sys_mode = await _read_system_llm_settings()

    # Read skill prompt, strip YAML frontmatter
    devserver_root = os.environ.get("DEVSERVER_ROOT")
    if devserver_root:
        root = Path(devserver_root)
    else:
        root = Path(__file__).resolve().parent.parent.parent.parent.parent
    skill_path = root / ".claude" / "skills" / "devtask" / "SKILL.md"
    if not skill_path.exists():
        raise HTTPException(500, "devtask skill not found")
    raw = skill_path.read_text()
    prompt = raw.split("---", 2)[-1].strip() if raw.startswith("---") else raw
    prompt = prompt.replace("$ARGUMENTS", body.description)

    try:
        text_content = await llm_client.complete(
            vendor=sys_vendor,
            model=sys_model,
            prompt=prompt,
            max_tokens=2048,
            json_mode=True,
            mode=sys_mode,
        )
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"System LLM error: {exc}")

    try:
        task = _extract_json_object(text_content)
    except ValueError as exc:
        raise HTTPException(
            502, f"Failed to parse devtask response as JSON ({sys_vendor}) — {exc}"
        )

    # Enforce DevServer defaults regardless of what the system LLM emitted:
    # Billing → Max (subscription), Skip Verification → true, model → latest
    # Claude Opus. These only fill in missing/empty values so an explicit user
    # request carried through the skill prompt still wins.
    if not task.get("claude_mode"):
        task["claude_mode"] = "max"
    if task.get("skip_verify") is None:
        task["skip_verify"] = True
    if not task.get("claude_model"):
        task["claude_model"] = "claude-opus-5"

    return task


# ─── DevPlan Skill ──────────────────────────────────────────────────────────

class GeneratePlanRequest(BaseModel):
    project_name: str
    description: str


@router.post("/generate-plan")
async def generate_plan(body: GeneratePlanRequest):
    """Call the system LLM with devplan skill prompt to generate a plan JSON.

    Returns ``{"plan_key": "...", "prompt": "..."}``.  When ``OBSIDIAN_FOLDER``
    is configured the prompt is also saved as ``<plan_key>.md`` in that folder.
    """
    # Read system LLM vendor/model/mode from settings table
    sys_vendor, sys_model, sys_mode = await _read_system_llm_settings()

    # Read skill prompt, strip YAML frontmatter
    devserver_root = os.environ.get("DEVSERVER_ROOT")
    if devserver_root:
        root = Path(devserver_root)
    else:
        root = Path(__file__).resolve().parent.parent.parent.parent.parent
    skill_path = root / ".claude" / "skills" / "devplan" / "SKILL.md"
    if not skill_path.exists():
        raise HTTPException(500, "devplan skill not found")
    raw = skill_path.read_text()
    prompt = raw.split("---", 2)[-1].strip() if raw.startswith("---") else raw

    # Replace $ARGUMENTS with "project_name description"
    arguments = f"{body.project_name} {body.description}"
    prompt = prompt.replace("$ARGUMENTS", arguments)

    try:
        text_content = await llm_client.complete(
            vendor=sys_vendor,
            model=sys_model,
            prompt=prompt,
            max_tokens=4096,
            json_mode=True,
            mode=sys_mode,
        )
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"System LLM error: {exc}")

    try:
        plan = _extract_json_object(text_content)
    except ValueError as exc:
        raise HTTPException(
            502, f"Failed to parse devplan response as JSON ({sys_vendor}) — {exc}"
        )

    # Save to Obsidian folder if configured
    obsidian_folder = settings.obsidian_folder
    if obsidian_folder:
        obsidian_path = Path(obsidian_folder)
        if obsidian_path.is_dir():
            plan_key = plan.get("plan_key", "PLAN-UNKNOWN")
            file_path = obsidian_path / f"{plan_key}.md"
            try:
                file_path.write_text(plan.get("prompt", ""), encoding="utf-8")
            except OSError:
                pass  # best-effort — don't fail the request

    return plan


# ─── Scheduled Jobs ─────────────────────────────────────────────────────────

class JobActionRequest(BaseModel):
    name: str


@router.get("/jobs")
async def list_jobs():
    return scheduler.get_all_jobs()


@router.post("/jobs/run")
async def run_job(body: JobActionRequest):
    if not scheduler.run_job_now(body.name):
        raise HTTPException(404, f"Job not found: {body.name}")
    return {"status": "ok", "name": body.name}


@router.post("/jobs/stop")
async def stop_job(body: JobActionRequest):
    if not scheduler.stop_job_now(body.name):
        raise HTTPException(409, f"Job not running or not found: {body.name}")
    return {"status": "ok", "name": body.name}


# Inter-task messaging endpoints moved to routes/pro_internal.py
# (/tasks/{task_key}/messages/send, /inbox, /thread, /sessions/list)


# ─── Context compaction ────────────────────────────────────────────────────

@router.post("/tasks/{task_key}/compact")
async def compact_task(task_key: str, reason: str = "manual"):
    """Summarise a task's transcript via the system LLM.

    Writes the result onto ``tasks.compacted_context`` and emits a
    ``context_compacted`` event. The next attempt will inject the
    summary as its sole context block (repo map/memory/reality signal
    are skipped).

    The caller is responsible for separately clearing ``session_id``
    or triggering a continuation. Invoking ``/tasks/<key>/continue``
    after ``/compact`` is the standard recovery path from the dashboard.
    """
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(404, f"Task {task_key} not found")
        result = await compaction.compact_task(
            db, task_id=task.id, reason=reason,
        )
    if not result["ok"]:
        raise HTTPException(502, result.get("error") or "compaction failed")
    return {
        "task_key": task_key,
        "chars_in": result["chars_in"],
        "chars_out": result["chars_out"],
        "compression_ratio": (
            round(result["chars_out"] / result["chars_in"], 3)
            if result["chars_in"] else None
        ),
    }


# ─── Goal Graph (recursive decomposition) ──────────────────────────────────

class ExpandRequest(BaseModel):
    max_depth: int | None = None
    enqueue: bool = False


@router.post("/goals/{node_id}/expand")
async def expand_goal(node_id: int, req: ExpandRequest | None = None):
    """Expand one Goal Graph node one level (atomicity check + plan-sketch).

    Leaves bind to a ``tasks`` row and optionally enqueue;
    composites get 3–7 child nodes. Never raises on LLM failure — the node
    degrades to a leaf.
    """
    req = req or ExpandRequest()
    kwargs: dict = {"enqueue": bool(req.enqueue)}
    if req.max_depth is not None:
        kwargs["max_depth"] = req.max_depth
    async with async_session() as db:
        result = await decomposer.expand_node(db, node_id, **kwargs)
    if not result.get("ok"):
        raise HTTPException(404 if "not found" in (result.get("reason") or "") else 502,
                            result.get("reason") or "expand failed")
    return result


@router.post("/goals/{node_id}/rollup")
async def rollup_goal(node_id: int):
    """Synthesise completed children into the parent's summary + 0–100 score."""
    async with async_session() as db:
        result = await decomposer.rollup_node(db, node_id)
    if not result.get("ok"):
        raise HTTPException(409, result.get("reason") or "rollup not ready")
    return result


# ─── Side-effect gate (human-in-the-loop) ──────────────────────────────────

class GateRequest(BaseModel):
    action: str
    payload: dict | None = None
    node_id: int | None = None


@router.post("/tasks/{task_key}/gate")
async def request_gate(task_key: str, req: GateRequest):
    """Agent-facing: classify a pending side-effecting action. Returns
    ``{"decision": "allow"}`` to proceed or ``{"decision": "blocked", ...}``
    (the agent must then stop — the task is suspended for human approval)."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if not task:
            raise HTTPException(404, f"Task {task_key} not found")
        return await side_effect_gate.raise_gate(
            db, task_id=task.id, task_key=task_key,
            action=req.action, payload=req.payload, node_id=req.node_id,
        )


@router.get("/decisions")
async def list_decisions(limit: int = 50):
    """List open side-effect decision points awaiting human resolution."""
    async with async_session() as db:
        return {"decisions": await side_effect_gate.list_open_decisions(db, limit=limit)}


class ResolveRequest(BaseModel):
    decision: str                       # 'approve' | 'reject' | 'edit'
    comment: str = ""
    edited_payload: dict | None = None
    resolved_by: str = "operator"


@router.post("/decisions/{decision_id}/resolve")
async def resolve_decision(decision_id: int, req: ResolveRequest):
    """Approve / reject / edit an open decision point and resume the task."""
    async with async_session() as db:
        result = await side_effect_gate.resolve_decision(
            db, decision_id=decision_id, decision=req.decision,
            comment=req.comment, edited_payload=req.edited_payload,
            resolved_by=req.resolved_by,
        )
    if not result.get("ok"):
        reason = result.get("reason") or "resolve failed"
        raise HTTPException(404 if "not found" in reason else 409, reason)
    return result


# ─── Skills (SKILL.md registry) ─────────────────────────────────────────────

@router.get("/skills")
async def list_skills():
    """List skills registered in the DB (synced from disk)."""
    async with async_session() as db:
        rows = (await db.execute(text(
            "SELECT id, name, description, domain, version, enabled, path, eval_pass_rate "
            "FROM skills ORDER BY name"
        ))).mappings().all()
    return {"skills": [dict(r) for r in rows]}


@router.post("/skills/sync")
async def sync_skills():
    """Re-scan the skills/ directory and upsert SKILL.md folders into the DB."""
    async with async_session() as db:
        return await skills_svc.sync_to_db(db)


# ─── Schedules (cron-ish jobs over scheduler.py) ────────────────────────────

class ScheduleCreate(BaseModel):
    name: str
    cron_expr: str = "@daily"
    task_id: int
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    cron_expr: str | None = None
    task_id: int | None = None
    enabled: bool | None = None


@router.get("/schedules")
async def list_schedules():
    async with async_session() as db:
        rows = (await db.execute(text(
            """
            SELECT s.id, s.name, s.cron_expr, s.task_id,
                   s.enabled, s.last_run_at, s.next_run_at,
                   t.task_key, t.title AS task_title, t.status AS task_status
            FROM schedules s
            LEFT JOIN tasks t ON t.id = s.task_id
            ORDER BY s.id
            """
        ))).mappings().all()
    return {"schedules": [dict(r) for r in rows]}


@router.post("/schedules")
async def create_schedule(req: ScheduleCreate):
    async with async_session() as db:
        task = (await db.execute(
            text("SELECT id FROM tasks WHERE id = :t"), {"t": req.task_id},
        )).fetchone()
        if not task:
            raise HTTPException(400, f"task {req.task_id} not found")
        row = (await db.execute(text(
            """
            INSERT INTO schedules (name, cron_expr, task_id, enabled)
            VALUES (:name, :cron, :tid, :en)
            RETURNING id
            """
        ), {"name": req.name, "cron": req.cron_expr,
            "tid": req.task_id, "en": req.enabled})).fetchone()
        await db.commit()
    await scheduler.reload_schedules()
    return {"id": row[0]}


@router.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, req: ScheduleUpdate):
    fields = {k: v for k, v in req.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "no fields to update")
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = schedule_id
    async with async_session() as db:
        res = await db.execute(
            text(f"UPDATE schedules SET {set_clause}, updated_at = NOW() WHERE id = :id"),
            fields,
        )
        await db.commit()
        if res.rowcount == 0:
            raise HTTPException(404, f"schedule {schedule_id} not found")
    await scheduler.reload_schedules()
    return {"id": schedule_id, "updated": list(fields.keys())}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int):
    async with async_session() as db:
        res = await db.execute(text("DELETE FROM schedules WHERE id = :id"), {"id": schedule_id})
        await db.commit()
        if res.rowcount == 0:
            raise HTTPException(404, f"schedule {schedule_id} not found")
    await scheduler.reload_schedules()
    return {"id": schedule_id, "deleted": True}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: int):
    """Fire a schedule immediately (advances its in-memory job to now)."""
    if scheduler.run_job_now(f"schedule:{schedule_id}"):
        return {"id": schedule_id, "status": "fired"}
    raise HTTPException(404, f"schedule {schedule_id} not registered (enabled?)")


# Webhook-fire endpoint moved to routes/pro_internal.py
# (POST /internal/webhooks/fire)
