"""Internal management API — called by FinCore TelegramController.

All endpoints are prefixed /internal and expect an X-Internal-Token header
matching INTERNAL_API_TOKEN env var (optional but recommended in production).
"""

import json as _json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, update

from config import settings
from models.base import async_session
from models.repo import Repo
from models.setting import Setting
from models.task import Task
from models.task_run import TaskRun
from services.queue_consumer import is_consumer_running
from services import night_cycle
from services import patch_ops
from services import scheduler

router = APIRouter(prefix="/internal")


# ─── Models ─────────────────────────────────────────────────────────────────

class ModeRequest(BaseModel):
    mode: str  # "autonomous" or "interactive"


class TaskKeyRequest(BaseModel):
    task_key: str


class NightCycleStartRequest(BaseModel):
    end_hour: int = 7


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


# ─── Night cycle ─────────────────────────────────────────────────────────────

@router.post("/night-cycle/start")
async def start_night_cycle(req: NightCycleStartRequest):
    """Start the night cycle with a given end hour (UTC)."""
    if req.end_hour < 0 or req.end_hour > 23:
        raise HTTPException(status_code=400, detail="end_hour must be 0\u201323")
    result = await night_cycle.start(end_hour=req.end_hour)
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


# ─── Patch Export (Option A) ─────────────────────────────────────────────────
#
# Three endpoints backing the "Patches" UI panel on the task detail page.
# All three are keyed by task_key (not task_id) so they match the task log
# endpoint above — the dashboard already has the key.

async def _resolve_task_for_patches(task_key: str) -> tuple[Task, Repo]:
    """Fetch a task + its repo by task_key, raising 404 on miss."""
    async with async_session() as db:
        res = await db.execute(select(Task).where(Task.task_key == task_key))
        task = res.scalar_one_or_none()
        if task is None:
            raise HTTPException(404, f"task {task_key!r} not found")
        repo = await db.get(Repo, task.repo_id)
        if repo is None:
            raise HTTPException(404, f"repo for task {task_key!r} not found")
        return task, repo


@router.get("/tasks/{task_key}/patches")
async def list_task_patches(task_key: str):
    """List existing patch files for a task.

    Returns whatever is currently on disk — does NOT regenerate. Used by
    the dashboard Patches panel to render the list + download buttons.
    """
    patchset = patch_ops.list_patches(task_key)
    return patchset.to_dict()


@router.post("/tasks/{task_key}/patches/generate")
async def regenerate_task_patches(task_key: str):
    """Regenerate patches for a task on demand.

    Safe to call multiple times — the patches directory is wiped and
    rebuilt from scratch. Runs against the bare repo, so it works even
    after the live worktree has been reset.
    """
    task, repo = await _resolve_task_for_patches(task_key)
    branch_name = f"agent/{task_key.replace(' ', '-').replace('/', '-').strip('-')}"
    patchset = await patch_ops.generate_patches(
        task_key=task_key,
        repo_name=repo.name,
        base_branch=repo.default_branch,
        branch_name=branch_name,
    )
    if not patchset.ok:
        raise HTTPException(400, patchset.error or "patch generation failed")
    return patchset.to_dict()


@router.get("/tasks/{task_key}/patches/file/{filename}")
async def download_task_patch(task_key: str, filename: str):
    """Stream a single patch (or the combined mbox) back as a download.

    The filename is validated by ``patch_ops.get_patch_path`` which
    rejects anything that doesn't look like a format-patch artefact —
    this is the guard against path traversal.
    """
    path = patch_ops.get_patch_path(task_key, filename)
    if path is None:
        raise HTTPException(404, f"patch {filename!r} not found")

    media_type = (
        "application/mbox"
        if filename.endswith(".mbox")
        else "text/x-patch"
    )
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=filename,
    )


# ─── DevTask Skill ──────────────────────────────────────────────────────────

class GenerateTaskRequest(BaseModel):
    description: str


@router.post("/generate-task")
async def generate_task(body: GenerateTaskRequest):
    """Call Claude API with devtask skill prompt to generate task JSON."""
    if not settings.anthropic_api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    # Read skill prompt, strip YAML frontmatter
    devserver_root = os.environ.get("DEVSERVER_ROOT")
    if devserver_root:
        root = Path(devserver_root)
    else:
        # Local dev: go up from routes/ -> src/ -> worker/ -> apps/ -> project root
        root = Path(__file__).resolve().parent.parent.parent.parent.parent
    skill_path = root / ".claude" / "skills" / "devtask" / "SKILL.md"
    if not skill_path.exists():
        raise HTTPException(500, "devtask skill not found")
    raw = skill_path.read_text()
    prompt = raw.split("---", 2)[-1].strip() if raw.startswith("---") else raw

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{
                    "role": "user",
                    "content": prompt.replace("$ARGUMENTS", body.description),
                }],
            },
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"Claude API error: {resp.status_code}")
        data = resp.json()

    text_content = data.get("content", [{}])[0].get("text", "")
    # Strip markdown fences if any
    cleaned = text_content.replace("```json", "").replace("```", "").strip()
    try:
        task = _json.loads(cleaned)
    except _json.JSONDecodeError:
        raise HTTPException(502, "Failed to parse devtask response as JSON")

    return task


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
