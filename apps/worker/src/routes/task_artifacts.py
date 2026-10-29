"""What a task produced and what the operator says to it.

Its log, its outcome forecast, its exported patches and its operator notes.
Task keys and file names arrive in URLs, so file-backed endpoints validate
them before touching the disk.
"""

import os
import time
from collections import deque

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from config import settings
from models.base import async_session
from models.task import Task
from services import patch_export, task_notes

router = APIRouter(prefix="/internal")


# ─── Task Log ────────────────────────────────────────────────────────────────


@router.get("/tasks/{task_key}/log")
async def task_log_tail(task_key: str, lines: int = 50):
    """Return last N lines from a task's log file."""
    if not patch_export.is_safe_task_key(task_key):
        raise HTTPException(400, f"invalid task key {task_key!r}")
    log_path = os.path.join(settings.log_dir, f"{task_key}.log")
    if not os.path.exists(log_path):
        return {"lines": []}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
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
    """Forecast a task's outcome from its repo's recent success rate.

    The response carries ``basis`` so the UI can label where the number comes from.
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

        pred = await outcome.predict_outcome_basic(db, task.repo_id)

    if len(_PREDICTION_CACHE) >= _PREDICTION_CACHE_MAX:
        oldest = min(_PREDICTION_CACHE, key=lambda k: _PREDICTION_CACHE[k][0])
        _PREDICTION_CACHE.pop(oldest, None)
    _PREDICTION_CACHE[task_key] = (time.monotonic(), pred)
    return {"prediction": pred}


# ─── Patch Export ───────────────────────────────────────────────────────────


@router.get("/tasks/{task_key}/patches")
async def list_task_patches(task_key: str):
    """The ``.patch`` files exported for a patch-mode task."""
    try:
        files = patch_export.list_patches(task_key)
    except patch_export.InvalidPatchPath as exc:
        raise HTTPException(400, str(exc)) from None
    return {"patches": [{"name": f.name, "size_bytes": f.size_bytes} for f in files]}


@router.get("/tasks/{task_key}/patches/{file_name:path}")
async def download_task_patch(task_key: str, file_name: str):
    # ``:path`` on purpose: a traversal attempt like ``../../x`` must reach the
    # validator and get a 400, not silently fall through to a 404.
    try:
        path = patch_export.resolve_patch_file(task_key, file_name)
    except patch_export.InvalidPatchPath as exc:
        raise HTTPException(400, str(exc)) from None
    if not path.is_file():
        raise HTTPException(404, f"No patch {file_name} for {task_key}")
    return FileResponse(path, media_type="text/x-patch", filename=path.name)


# ─── Task Notes ─────────────────────────────────────────────────────────────


class NoteRequest(BaseModel):
    body: str


@router.get("/tasks/{task_key}/notes")
async def list_task_notes(task_key: str):
    async with async_session() as db:
        return {"notes": await task_notes.list_notes(db, task_key=task_key)}


@router.post("/tasks/{task_key}/notes")
async def post_task_note(task_key: str, req: NoteRequest):
    """Leave a note for the agent; a running task reads it in its next attempt."""
    async with async_session() as db:
        try:
            note = await task_notes.post_note(db, task_key=task_key, body=req.body)
        except task_notes.TaskNotFoundError:
            raise HTTPException(404, f"Task {task_key} not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return {"id": note.id, "will_be_delivered": note.will_be_delivered}
