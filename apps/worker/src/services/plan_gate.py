"""Plan Gate: interactive tasks stop for a human-approved plan.

For ``mode='interactive'`` the agent first runs read-only with a small turn
budget and answers with a JSON plan. The plan is stored on the run
(``task_runs.plan_json``), announced with a ``plan_pending`` event, and the
task waits until the operator approves or rejects it in the dashboard
(``tasks.plan_approved_at`` / ``plan_rejected_at``) or the wait times out.

An approved plan is injected into the implementation prompt, and its
``files_to_touch`` becomes the PR-preflight allow-list, so the agent cannot
quietly expand the scope it was approved for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy import text

from models.base import async_session
from services import agent_backends, app_settings, git_ops
from services.notify import notify
from services.runner import agent_call, store
from services.runner.context import TaskContext

logger = logging.getLogger(__name__)

PLAN_TOOLS = "Read,Glob,Grep"
PLAN_MAX_TURNS = 15
PLAN_TIMEOUT_MINUTES = 15
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 3600
POLL_SECONDS = 5.0
_MAX_ITEMS = 50
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class PlanParseError(ValueError):
    """The agent's reply contained nothing usable as a plan."""


@dataclass(frozen=True)
class Plan:
    summary: str
    steps: tuple[str, ...] = ()
    files_to_touch: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "summary": self.summary,
            "steps": list(self.steps),
            "files_to_touch": list(self.files_to_touch),
            "risks": list(self.risks),
        }

    def render_for_prompt(self) -> str:
        lines = ["## Approved Plan", "The operator approved this plan. Implement it.", ""]
        lines += [f"Summary: {self.summary}"]
        if self.steps:
            lines += ["", "Steps:", *(f"{n}. {step}" for n, step in enumerate(self.steps, 1))]
        if self.files_to_touch:
            lines += ["", "Files you may change (others are out of scope):"]
            lines += [f"- {path}" for path in self.files_to_touch]
        if self.risks:
            lines += ["", "Known risks:", *(f"- {risk}" for risk in self.risks)]
        return "\n".join(lines)


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = (str(item).strip() for item in value if isinstance(item, str | int | float))
    return tuple(item for item in items if item)[:_MAX_ITEMS]


def parse_plan(reply: str) -> Plan:
    """Parse the agent's reply into a :class:`Plan`.

    A well-formed JSON object yields every field. When the JSON is missing or
    malformed but the reply has prose, the first paragraph is salvaged as the
    summary so the operator still has something to judge.
    """
    reply = (reply or "").strip()
    if not reply:
        raise PlanParseError("empty plan reply")

    match = _JSON_OBJECT.search(reply)
    data: object = None
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict) and str(data.get("summary", "")).strip():
        return Plan(
            summary=str(data["summary"]).strip()[:2000],
            steps=_string_list(data.get("steps")),
            files_to_touch=_string_list(data.get("files_to_touch")),
            risks=_string_list(data.get("risks")),
        )

    prose = _JSON_OBJECT.sub("", reply).strip() or reply
    first_paragraph = prose.split("\n\n", 1)[0].strip()
    if not first_paragraph:
        raise PlanParseError("plan reply has no summary")
    return Plan(summary=first_paragraph[:2000])


def _planning_prompt(ctx: TaskContext) -> str:
    task = ctx.task
    return "\n".join(
        [
            f"You are planning a change to repository {ctx.repo.name}. Do NOT modify any file.",
            f"Task: {task.key} - {task.title}",
            "",
            "## Task Description",
            task.description or "(no description provided)",
            "",
            "## Acceptance Criteria",
            task.acceptance or "(none specified)",
            "",
            "Read the code you need, then reply with ONLY a JSON object:",
            '{"summary": "...", "steps": ["..."], "files_to_touch": ["path/..."],'
            ' "risks": ["..."]}',
            "files_to_touch lists every repository-relative path you expect to change.",
        ]
    )


async def _draft_plan(ctx: TaskContext, *, run_id: int) -> Plan | None:
    result = await agent_call.run_agent(
        agent_call.AgentInvocation(
            backend=agent_backends.get_backend(ctx.task.vendor),
            worktree_path=ctx.worktree_path,
            prompt=_planning_prompt(ctx),
            model=ctx.model,
            allowed_tools=PLAN_TOOLS,
            session_id=None,
            timeout_minutes=PLAN_TIMEOUT_MINUTES,
            task_id=ctx.task.id,
            run_id=run_id,
            billing_mode=ctx.billing_mode,
            max_turns=PLAN_MAX_TURNS,
        )
    )
    if result.exit_code != 0:
        ctx.log.note("plan_gate", f"planning run failed (exit {result.exit_code})")
        return None
    if await git_ops.is_dirty(ctx.worktree_path):
        ctx.log.note("plan_gate", "planning run modified files; refusing the plan")
        return None
    try:
        return parse_plan(result.result)
    except PlanParseError as exc:
        ctx.log.note("plan_gate", f"could not parse a plan: {exc}")
        return None


async def _decision(task_id: int) -> str | None:
    async with async_session() as db:
        row = (
            await db.execute(
                text("SELECT plan_approved_at, plan_rejected_at FROM tasks WHERE id = :id"),
                {"id": task_id},
            )
        ).one()
    if row.plan_rejected_at is not None:
        return "rejected"
    if row.plan_approved_at is not None:
        return "approved"
    return None


async def wait_for_decision(task_id: int, *, timeout_seconds: float) -> str:
    """Poll the task row; return ``approved``, ``rejected`` or ``timeout``."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        decision = await _decision(task_id)
        if decision is not None:
            return decision
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        await asyncio.sleep(min(POLL_SECONDS, remaining))


async def await_approved_plan(ctx: TaskContext) -> Plan | None:
    """Draft a plan, publish it and wait for the operator. None = task blocked."""
    task = ctx.task
    await store.set_task_columns(task.id, plan_approved_at=None, plan_rejected_at=None)
    run_id = await store.start_run(task.id, attempt=0, branch=ctx.branch_name)
    plan = await _draft_plan(ctx, run_id=run_id)
    if plan is None:
        await store.finish_run(run_id, status="failed", error_log="no usable plan")
        await store.set_task_status(task.id, "blocked")
        return None

    await store.finish_run(run_id, status="success", plan_json=plan.to_json())
    await store.emit_event(task.id, run_id, "plan_pending", {"plan": plan.to_json()})
    await notify.text(f"📋 *{task.key}* plan ready for review: {plan.summary[:300]}")
    async with async_session() as db:
        timeout = await app_settings.get_int_setting(
            db, "plan_gate_timeout_seconds", DEFAULT_APPROVAL_TIMEOUT_SECONDS
        )
    decision = await wait_for_decision(task.id, timeout_seconds=timeout)

    if decision == "approved":
        await store.emit_event(task.id, run_id, "plan_approved", {})
        return plan
    await store.emit_event(task.id, run_id, "plan_rejected", {"reason": decision})
    await store.set_task_status(task.id, "blocked")
    ctx.log.note("plan_gate", f"plan {decision} — task blocked")
    return None
