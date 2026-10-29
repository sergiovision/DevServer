"""Worker-wide controls: status, queue pause/resume, mode, background jobs,
schedules, skills and side-effect gate decisions.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text

from models.base import async_session
from models.setting import Setting
from models.task import Task
from routes.tasks import ModeRequest
from services import (
    scheduler,
    side_effect_gate,
)
from services import skills as skills_svc
from services.queue_consumer import is_consumer_running

router = APIRouter(prefix="/internal")


# ─── Status ─────────────────────────────────────────────────────────────────


@router.get("/status")
async def worker_status():
    """Full devserver status: mode, paused, queue stats, active tasks."""
    worker_running = is_consumer_running()

    async with async_session() as db:
        # Active tasks (running or verifying)
        res = await db.execute(select(Task).where(Task.status.in_(["running", "verifying"])))
        active = res.scalars().all()

        # Pending/queued tasks
        res2 = await db.execute(
            select(Task).where(Task.status.in_(["pending", "queued"])).order_by(Task.priority)
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
            setting.updated_at = datetime.now(UTC)
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
            setting.updated_at = datetime.now(UTC)
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
            setting.updated_at = datetime.now(UTC)
        else:
            db.add(Setting(key="mode", value=mode))
        await db.commit()

    return {"mode": mode, "message": f"Mode set to: {mode}"}


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
            db,
            task_id=task.id,
            task_key=task_key,
            action=req.action,
            payload=req.payload,
            node_id=req.node_id,
        )


@router.get("/decisions")
async def list_decisions(limit: int = 50):
    """List open side-effect decision points awaiting human resolution."""
    async with async_session() as db:
        return {"decisions": await side_effect_gate.list_open_decisions(db, limit=limit)}


class ResolveRequest(BaseModel):
    decision: str  # 'approve' | 'reject' | 'edit'
    comment: str = ""
    edited_payload: dict | None = None
    resolved_by: str = "operator"


@router.post("/decisions/{decision_id}/resolve")
async def resolve_decision(decision_id: int, req: ResolveRequest):
    """Approve / reject / edit an open decision point and resume the task."""
    async with async_session() as db:
        result = await side_effect_gate.resolve_decision(
            db,
            decision_id=decision_id,
            decision=req.decision,
            comment=req.comment,
            edited_payload=req.edited_payload,
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
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, name, description, domain, version, enabled, path, eval_pass_rate "
                        "FROM skills ORDER BY name"
                    )
                )
            )
            .mappings()
            .all()
        )
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
        rows = (
            (
                await db.execute(
                    text(
                        """
            SELECT s.id, s.name, s.cron_expr, s.task_id,
                   s.enabled, s.last_run_at, s.next_run_at,
                   t.task_key, t.title AS task_title, t.status AS task_status
            FROM schedules s
            LEFT JOIN tasks t ON t.id = s.task_id
            ORDER BY s.id
            """
                    )
                )
            )
            .mappings()
            .all()
        )
    return {"schedules": [dict(r) for r in rows]}


@router.post("/schedules")
async def create_schedule(req: ScheduleCreate):
    async with async_session() as db:
        task = (
            await db.execute(
                text("SELECT id FROM tasks WHERE id = :t"),
                {"t": req.task_id},
            )
        ).fetchone()
        if not task:
            raise HTTPException(400, f"task {req.task_id} not found")
        row = (
            await db.execute(
                text(
                    """
            INSERT INTO schedules (name, cron_expr, task_id, enabled)
            VALUES (:name, :cron, :tid, :en)
            RETURNING id
            """
                ),
                {"name": req.name, "cron": req.cron_expr, "tid": req.task_id, "en": req.enabled},
            )
        ).fetchone()
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
