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
from sqlalchemy import select, text, update

from config import settings
from models.base import async_session
from models.repo import Repo
from models.setting import Setting
from models.task import Task
from models.task_run import TaskRun
from services.queue_consumer import is_consumer_running
from services import compaction
from services import git_ops
from services import llm_client
from services import scheduler

router = APIRouter(prefix="/internal")


# ─── Models ─────────────────────────────────────────────────────────────────

class ModeRequest(BaseModel):
    mode: str  # "autonomous" or "interactive"


class TaskKeyRequest(BaseModel):
    task_key: str


class ContinueTaskRequest(BaseModel):
    model: str | None = None
    mode: str | None = None  # "max" or "api"


    # NightCycleStartRequest moved to routes/pro_internal.py


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


# ─── Refresh Git ───────────────────────────────────────────────────────────

@router.post("/repos/{repo_id}/refresh-git")
async def refresh_git(repo_id: int):
    """Clone or fetch a repo's bare repo and worktree."""
    async with async_session() as db:
        repo = await db.get(Repo, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")

    result = await git_ops.refresh_repo(
        repo_name=repo.name,
        clone_url=repo.clone_url,
        default_branch=repo.default_branch,
        gitea_token=repo.gitea_token or None,
    )
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


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
        repo = await db.get(Repo, task.repo_id)
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
    # Read system LLM vendor/model from settings table
    async with async_session() as db:
        vendor_row = await db.execute(
            select(Setting).where(Setting.key == "system_llm_vendor")
        )
        model_row = await db.execute(
            select(Setting).where(Setting.key == "system_llm_model")
        )
        vendor_setting = vendor_row.scalar_one_or_none()
        model_setting = model_row.scalar_one_or_none()

    # Settings store values as JSON strings — strip the outer quotes
    sys_vendor = "glm"
    sys_model = "glm-5.1"
    if vendor_setting and vendor_setting.value:
        v = vendor_setting.value
        sys_vendor = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)
    if model_setting and model_setting.value:
        v = model_setting.value
        sys_model = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)

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
            max_tokens=1024,
        )
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"System LLM error: {exc}")

    # Strip markdown fences if any
    cleaned = text_content.replace("```json", "").replace("```", "").strip()
    try:
        task = _json.loads(cleaned)
    except _json.JSONDecodeError:
        raise HTTPException(502, "Failed to parse devtask response as JSON")

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
    # Read system LLM vendor/model from settings table
    async with async_session() as db:
        vendor_row = await db.execute(
            select(Setting).where(Setting.key == "system_llm_vendor")
        )
        model_row = await db.execute(
            select(Setting).where(Setting.key == "system_llm_model")
        )
        vendor_setting = vendor_row.scalar_one_or_none()
        model_setting = model_row.scalar_one_or_none()

    sys_vendor = "glm"
    sys_model = "glm-5.1"
    if vendor_setting and vendor_setting.value:
        v = vendor_setting.value
        sys_vendor = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)
    if model_setting and model_setting.value:
        v = model_setting.value
        sys_model = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)

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
            max_tokens=2048,
        )
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"System LLM error: {exc}")

    # Strip markdown fences if any
    cleaned = text_content.replace("```json", "").replace("```", "").strip()
    try:
        plan = _json.loads(cleaned)
    except _json.JSONDecodeError:
        raise HTTPException(502, "Failed to parse devplan response as JSON")

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


# Webhook-fire endpoint moved to routes/pro_internal.py
# (POST /internal/webhooks/fire)
