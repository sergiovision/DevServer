"""Propose → approve → execute, for the Ask Agent panel.

**The constraint this design is built around:** a headless ``claude -p``
subprocess cannot be paused mid-tool and resumed after an out-of-band approval.
There is no interactive permission prompt in print mode — a tool is allowed or
denied when the process is spawned. Any design that holds the process open
while a human decides is either a deadlock or ``--dangerously-skip-permissions``
wearing a disguise.

So approval gates a **separate follow-up turn**, the same shape
``services/side_effect_gate.py`` already uses for task runs:

1. The assistant calls ``propose_action``, which performs nothing — it records a
   proposal and returns "awaiting approval". The agent then ends its turn and
   the stream closes.
2. The operator clicks Run. The **worker** executes the action; the agent never
   does, and never had the tool to.
3. The output streams back, and the agent is resumed with the outcome as its
   next user message.

Two deliberate boundaries:

- **The model supplies a key, never a path and never arguments.** Scripts come
  from ``ALLOWED_SCRIPTS`` below. Arbitrary shell is out of scope: on a worker
  with no authentication that is a remote-code-execution primitive.
- **The registry is in memory, not a table.** Proposals are only meaningful
  while the operator's tab is open, and a table would mean a migration plus
  cleanup. A worker restart yields a clean "this proposal expired" 404.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import config
from services import proc as proc_util

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_SCRIPTS",
    "PendingAction",
    "create",
    "decide",
    "describe_actions",
    "execute_and_resume",
    "get",
    "list_pending",
]

#: The complete set of things the assistant may propose running. The model
#: picks a key; the path is ours. Anything not in here cannot be proposed.
ALLOWED_SCRIPTS: dict[str, dict[str, str]] = {
    "dbrestart": {
        "path": "scripts/dbrestart.sh",
        "summary": "Restart PostgreSQL and verify it came back up",
        "risk": "medium",
    },
    "migrate": {
        "path": "scripts/migrate.sh",
        "summary": "Apply pending database migrations",
        "risk": "medium",
    },
    "restart": {
        "path": "scripts/restart.sh",
        "summary": "Restart the DevServer worker and web processes",
        "risk": "high",
    },
    "backup": {
        "path": "scripts/devserver-backup.sh",
        "summary": "Back up the database, settings and .env to backups/",
        "risk": "low",
    },
}

_TTL_SECONDS = 30 * 60
_MAX_PENDING = 200
_EXEC_TIMEOUT = 300
_OUTPUT_TAIL_CHARS = 4000


@dataclass
class PendingAction:
    id: str
    session_id: str
    kind: str
    summary: str
    params: dict
    risk: str = "medium"
    status: str = "pending"  # pending|approved|rejected|executed|failed|expired
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    comment: str = ""
    output: str = ""
    exit_code: int | None = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "summary": self.summary,
            "params": self.params,
            "risk": self.risk,
            "status": self.status,
            "created_at": self.created_at,
            "exit_code": self.exit_code,
        }


_REGISTRY: dict[str, PendingAction] = {}


def _sweep() -> None:
    cutoff = time.time() - _TTL_SECONDS
    for key in [k for k, a in _REGISTRY.items() if a.created_at < cutoff]:
        _REGISTRY.pop(key, None)
    # Backstop against a runaway proposer.
    while len(_REGISTRY) > _MAX_PENDING:
        oldest = min(_REGISTRY.values(), key=lambda a: a.created_at)
        _REGISTRY.pop(oldest.id, None)


def describe_actions() -> dict:
    """What may be proposed — surfaced to the agent and the capabilities API."""
    return {
        "run_script": {
            "scripts": {
                key: {"summary": meta["summary"], "risk": meta["risk"]}
                for key, meta in ALLOWED_SCRIPTS.items()
            }
        },
        "task_create": {"note": "creates a DevServer task after approval"},
    }


def create(*, session_id: str, kind: str, summary: str, params: dict) -> PendingAction:
    """Record a proposal. Raises ValueError if it is not something we allow."""
    _sweep()
    kind = (kind or "").strip()
    params = params or {}
    risk = "medium"

    if kind == "run_script":
        key = str(params.get("script") or "").strip()
        meta = ALLOWED_SCRIPTS.get(key)
        if meta is None:
            raise ValueError(
                f"unknown script {key!r}. Allowed: {', '.join(sorted(ALLOWED_SCRIPTS))}"
            )
        params = {"script": key}  # drop anything else the model tried to pass
        risk = meta["risk"]
        summary = summary or meta["summary"]
    elif kind == "task_create":
        if not str(params.get("title") or "").strip():
            raise ValueError("task_create needs a title")
        risk = "low"
    else:
        raise ValueError(f"unsupported action kind {kind!r}. Allowed: run_script, task_create")

    action = PendingAction(
        id=secrets.token_urlsafe(12),
        session_id=session_id or "",
        kind=kind,
        summary=summary or kind,
        params=params,
        risk=risk,
    )
    _REGISTRY[action.id] = action
    logger.info("Assistant proposed %s (%s): %s", kind, action.id, action.summary)
    return action


def get(action_id: str) -> PendingAction | None:
    _sweep()
    return _REGISTRY.get(action_id)


def list_pending(session_id: str | None = None) -> list[PendingAction]:
    _sweep()
    return [
        a
        for a in _REGISTRY.values()
        if a.status == "pending" and (session_id is None or a.session_id == session_id)
    ]


def decide(action_id: str, *, decision: str, comment: str = "") -> PendingAction:
    """Mark a proposal approved or rejected. Raises KeyError / ValueError."""
    _sweep()
    action = _REGISTRY.get(action_id)
    if action is None:
        raise KeyError(action_id)
    if action.status != "pending":
        raise ValueError(f"this proposal was already {action.status}")
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    action.status = "approved" if decision == "approve" else "rejected"
    action.decided_at = time.time()
    action.comment = comment or ""
    return action


# ── Execution ────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    root = os.environ.get("DEVSERVER_ROOT")
    if root and Path(root).is_dir():
        return Path(root)
    # apps/worker/src/services/… → repo root
    return Path(__file__).resolve().parents[4]


def _in_container() -> bool:
    """Best-effort container detection.

    It matters: inside the worker container, `dbrestart.sh` cannot restart the
    *host's* Postgres without a Docker socket. Telling the operator that is far
    better than running something that silently does nothing.

    The Database card's connection probe needs the same answer, so the
    predicate lives in `config` — two copies would be free to disagree about
    which side of the boundary this process is on.
    """
    return config.in_container()


async def _run_script(action: PendingAction) -> AsyncIterator[dict]:
    key = str(action.params.get("script") or "")
    meta = ALLOWED_SCRIPTS.get(key)
    if meta is None:  # unreachable via create(), kept as a hard gate
        yield {"type": "error", "message": f"script {key!r} is not allowed", "retryable": False}
        action.status = "failed"
        return

    root = _repo_root()
    script = (root / meta["path"]).resolve()
    try:
        script.relative_to(root.resolve())
    except ValueError:
        yield {
            "type": "error",
            "message": "script path escapes the project root",
            "retryable": False,
        }
        action.status = "failed"
        return
    if not script.is_file():
        yield {
            "type": "error",
            "message": (
                f"{meta['path']} is not present in this deployment. In Docker the "
                f"scripts directory is not copied into the worker image — run it "
                f"on the host instead: ./{meta['path']}"
            ),
            "retryable": False,
        }
        action.status = "failed"
        return

    if _in_container() and key in {"dbrestart", "restart"}:
        yield {
            "type": "error",
            "message": (
                "The worker is running inside a container and cannot restart "
                f"host services from here. Run this on the host: ./{meta['path']}"
            ),
            "retryable": False,
        }
        action.status = "failed"
        return

    bash = shutil.which("bash") or "/bin/bash"
    yield {"type": "action_output", "text": f"$ {meta['path']}\n"}

    collected: list[str] = []
    exit_code = -1
    try:
        async for channel, line in proc_util.run_streamed(
            [bash, str(script)],
            cwd=str(root),
            timeout=_EXEC_TIMEOUT,
            stdin_input=b"",
        ):
            if channel == "exit":
                exit_code = int(line or -1)
                continue
            collected.append(line)
            yield {"type": "action_output", "text": line}
    except TimeoutError:
        yield {
            "type": "error",
            "message": f"{meta['path']} timed out after {_EXEC_TIMEOUT}s",
            "retryable": True,
        }
        action.status = "failed"
        return
    except Exception as exc:
        logger.exception("Assistant action failed")
        yield {"type": "error", "message": str(exc), "retryable": False}
        action.status = "failed"
        return

    action.exit_code = exit_code
    action.output = "".join(collected)[-_OUTPUT_TAIL_CHARS:]
    action.status = "executed" if exit_code == 0 else "failed"
    yield {
        "type": "action_output",
        "text": f"\n[exit {exit_code}]\n",
    }


async def _run_task_create(action: PendingAction) -> AsyncIterator[dict]:
    """Create a DevServer task through the same endpoint everything else uses.

    Deliberately not a direct INSERT: routing through the normal create path
    keeps validation, key generation and enqueueing in one place.
    """
    try:
        from routes.tasks import CreateTaskRequest, create_task

        params = dict(action.params)
        body = CreateTaskRequest(
            title=str(params.get("title") or "").strip(),
            description=str(params.get("description") or ""),
            repo_id=params.get("repo_id"),
            task_type=str(params.get("task_type") or "coding"),
            enqueue=bool(params.get("enqueue", False)),
            created_by="assistant",
        )
        result = await create_task(body)
    except Exception as exc:
        logger.exception("Assistant task_create failed")
        action.status = "failed"
        yield {"type": "error", "message": f"could not create the task: {exc}", "retryable": False}
        return

    action.status = "executed"
    action.exit_code = 0
    action.output = (
        f"Created task {result.get('task_key')} (id {result.get('task_id')}), "
        f"status {result.get('status')}, enqueued={result.get('enqueued')}."
    )
    yield {"type": "action_output", "text": action.output + "\n"}


async def execute_and_resume(
    action: PendingAction,
    *,
    session=None,
    follow_up: bool = True,
    agent_engine=None,
) -> AsyncIterator[dict]:
    """Run an approved action (or acknowledge a rejection), then resume the agent.

    The agent process that proposed this has long since exited — it learns the
    outcome as the first user message of a fresh, resumed turn.
    """
    if action.status == "rejected":
        outcome = (
            f'The action you proposed ("{action.summary}") was REJECTED by the '
            f"operator. Do not perform it. "
            f"{('Their comment: ' + action.comment) if action.comment else ''}"
            " Suggest an alternative, or ask what they would prefer."
        )
    else:
        runner = _run_script if action.kind == "run_script" else _run_task_create
        try:
            async for event in runner(action):
                yield event
        finally:
            # Cancelled or closed mid-run (operator closed the tab, stream
            # limit): never leave the action "approved" forever.
            if action.status == "approved":
                action.status = "failed"
                if not action.output:
                    action.output = "interrupted before completion"
        tail = (action.output or "").strip()[-2000:]
        verb = "succeeded" if action.status == "executed" else "FAILED"
        outcome = (
            f'The action you proposed ("{action.summary}") was APPROVED and has '
            f"now been run by DevServer. It {verb} with exit code "
            f"{action.exit_code}.\n\nOutput:\n{tail or '(no output)'}\n\n"
            "Tell the operator what this means and what, if anything, to do next."
        )

    if not follow_up or session is None or agent_engine is None:
        yield {
            "type": "done",
            "stop_reason": action.status,
            "text": "",
            "session_id": getattr(session, "cli_session_id", None),
            "awaiting": None,
        }
        return

    from routes.assistant import _read_system_llm

    llm = await _read_system_llm()
    async for event in agent_engine.stream_agent_turn(
        session=session,
        user_text=outcome,
        system_prompt="",  # resumed turn — the CLI still has it
        vendor=llm.vendor,
        model=llm.model,
        billing_mode=llm.mode,
    ):
        yield event
