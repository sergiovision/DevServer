"""Inter-task messaging: send, inbox, thread and the live-session directory.

Agents call these mid-run (curl against ``$DEVSERVER_WORKER_URL``); the
dashboard calls them through ``/api/tasks/<id>/messages``. The reserved key
``operator`` addresses the human; it is **never** accepted as a sender on the
agent lane (``/tasks/{task_key}/messages/send``), because agents are told that
operator messages override their task. Messages *from* the operator go through
the separate ``/operator/messages/send`` endpoint, which checks
``X-Internal-Token`` against ``INTERNAL_API_TOKEN``.

Trade-off: with ``INTERNAL_API_TOKEN`` unset (the default) the operator
endpoint still works so an unconfigured deployment keeps working, and only a
warning is logged — anything that can reach the worker port could then post as
the operator. Agents are only ever shown the per-task lane, and the worker port
must never be exposed; set the token to close the gap.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from models.base import async_session
from routes.assistant import _require_internal_token
from services import task_messaging

router = APIRouter(prefix="/internal")


class MessageSendRequest(BaseModel):
    to_task_key: str
    body: str
    subject: str | None = None
    kind: str = "note"  # 'note' | 'request' | 'response' | 'handoff'
    payload: dict | None = None


async def _send(from_task_key: str, req: MessageSendRequest) -> dict:
    try:
        async with async_session() as db:
            msg = await task_messaging.send_message(
                db,
                from_task_key=from_task_key,
                to_task_key=req.to_task_key,
                body=req.body,
                subject=req.subject,
                kind=req.kind,
                payload=req.payload,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "message": msg}


@router.post("/tasks/{task_key}/messages/send")
async def messaging_send(task_key: str, req: MessageSendRequest):
    """Send a message FROM ``task_key`` to ``req.to_task_key``.

    ``operator`` is refused as a sender (any case): it would let a task forge
    an operator instruction. Use ``/operator/messages/send`` for that.
    """
    if task_key.strip().lower() == task_messaging.OPERATOR_KEY:
        raise HTTPException(
            403, "'operator' cannot send on the task lane — use /internal/operator/messages/send"
        )
    return await _send(task_key, req)


@router.post("/operator/messages/send")
async def messaging_send_as_operator(req: MessageSendRequest, request: Request):
    """Send a message FROM the human operator (dashboard, Telegram ``/reply``).

    Guarded by ``X-Internal-Token`` when ``INTERNAL_API_TOKEN`` is set; with
    the token unset it is allowed with a warning (see the module docstring).
    """
    _require_internal_token(request, purpose="operator message")
    return await _send(task_messaging.OPERATOR_KEY, req)


@router.get("/tasks/{task_key}/messages/inbox")
async def messaging_inbox(
    task_key: str,
    limit: int = 20,
    include_read: bool = False,
    mark_read: bool = True,
):
    """Messages addressed to ``task_key``. Default drains unread; ``mark_read=false`` peeks."""
    async with async_session() as db:
        msgs = await task_messaging.read_inbox(
            db,
            task_key=task_key,
            mark_read=mark_read,
            limit=limit,
            include_read=include_read,
        )
    return {"count": len(msgs), "messages": msgs}


@router.get("/tasks/{task_key}/messages/thread")
async def messaging_thread(task_key: str, limit: int = 50):
    """The task's conversation in both directions, oldest first."""
    async with async_session() as db:
        msgs = await task_messaging.thread(db, task_key=task_key, limit=limit)
    return {"count": len(msgs), "messages": msgs}


@router.get("/sessions/list")
async def messaging_sessions(limit: int = 50):
    """Live tasks an agent can message (pending, queued, running, verifying, blocked)."""
    async with async_session() as db:
        sessions = await task_messaging.list_sessions(db, limit=limit)
    return {"count": len(sessions), "sessions": sessions}
