"""Spec → Plan → Implement gate for interactive-mode tasks.

For tasks with ``mode='interactive'``, we split execution into two phases:

    1. **Plan phase.**   Ask Claude to produce a structured plan (JSON) that
       lists the files it intends to touch, the approach, the risks, and the
       steps. No file modifications. ``--max-turns`` is capped low.
    2. **Approval gate.** The plan is stored in ``task_runs.plan_json``, a
       ``plan_pending`` event is emitted, and the worker polls the database
       for a ``plan_approved_at`` timestamp on the task row. A REST endpoint
       in the Next.js dashboard sets this column when the human clicks
       Approve.
    3. **Implement phase.**  The approved plan is injected verbatim into the
       implementation prompt so the agent is bound to its own contract.

The plan JSON shape is deliberately narrow — we only need enough structure
for the PR preflight and the dashboard to render it:

    {
        "summary": str,
        "approach": str,
        "steps": [ {"n": int, "desc": str} ],
        "files_to_touch": [str],
        "risks": [str],
        "acceptance_check": str
    }

If Claude returns malformed JSON we still try to salvage a ``summary`` field
so the human has *something* to approve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# How often (seconds) we poll for plan_approved_at.
POLL_INTERVAL_SECONDS = 5
# How long (seconds) we wait before auto-blocking an unapproved plan.
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 3600  # 1 hour


# ── Prompt builders ───────────────────────────────────────────────────────────

PLAN_PROMPT_TEMPLATE = """You are an autonomous coding agent in PLANNING MODE.

Repository: {repo_name}
Task: {task_key} - {title}

## Task Description
{description}

## Acceptance Criteria
{acceptance}

## YOUR JOB RIGHT NOW
Do NOT modify any files. Your only job is to produce a JSON plan that will be
reviewed by a human. After approval, a separate execution phase will actually
write the code.

Explore the repository (read files, grep, check dependencies) then output
*exactly one* JSON object with this shape and NOTHING else:

```json
{{
  "summary": "one-sentence description of the change",
  "approach": "2-4 sentence description of how you intend to implement it",
  "steps": [
    {{"n": 1, "desc": "..."}},
    {{"n": 2, "desc": "..."}}
  ],
  "files_to_touch": ["relative/path/one.ext", "relative/path/two.ext"],
  "risks": ["short risk #1", "short risk #2"],
  "acceptance_check": "how you will know the acceptance criteria are satisfied"
}}
```

Constraints on the plan:
- files_to_touch MUST list every file you intend to create or modify. Anything
  not listed here will be considered scope creep during review.
- risks should call out anything non-obvious (breaking changes, migration
  dependencies, test gaps).
- Keep every field terse — this is for a human to skim, not to read fully.

Output the JSON and stop. Do not add commentary, do not wrap in prose, do not
include markdown before or after the JSON block.
"""


def build_plan_prompt(
    repo_name: str,
    task_key: str,
    title: str,
    description: str,
    acceptance: str,
) -> str:
    return PLAN_PROMPT_TEMPLATE.format(
        repo_name=repo_name,
        task_key=task_key,
        title=title,
        description=description or "(no description provided)",
        acceptance=acceptance or "(none specified)",
    )


# ── Plan parsing ──────────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def parse_plan_output(raw: str) -> dict[str, Any]:
    """Extract the JSON plan from Claude's output.

    Returns a dict even on failure — populates ``_parse_error`` instead of
    raising so the dashboard can still show something.
    """
    if not raw:
        return {"_parse_error": "empty plan output", "summary": "(empty)"}

    # Try the whole thing first.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return _normalize_plan(parsed)
    except json.JSONDecodeError:
        pass

    # Fall back to the largest {...} block.
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return _normalize_plan(parsed)
        except json.JSONDecodeError as exc:
            logger.warning("plan JSON parse failed: %s", exc)
            return {
                "_parse_error": f"json decode: {exc}",
                "summary": raw[:200],
                "raw_tail": raw[-500:],
            }

    return {
        "_parse_error": "no JSON object found",
        "summary": raw[:200],
        "raw_tail": raw[-500:],
    }


def _normalize_plan(d: dict[str, Any]) -> dict[str, Any]:
    """Coerce the plan dict into the canonical shape with safe defaults."""
    out: dict[str, Any] = {
        "summary": str(d.get("summary") or "").strip() or "(no summary)",
        "approach": str(d.get("approach") or "").strip(),
        "steps": [],
        "files_to_touch": [],
        "risks": [],
        "acceptance_check": str(d.get("acceptance_check") or "").strip(),
    }

    raw_steps = d.get("steps") or []
    if isinstance(raw_steps, list):
        for i, s in enumerate(raw_steps, start=1):
            if isinstance(s, dict):
                out["steps"].append({
                    "n": int(s.get("n") or i),
                    "desc": str(s.get("desc") or "").strip(),
                })
            elif isinstance(s, str):
                out["steps"].append({"n": i, "desc": s.strip()})

    raw_files = d.get("files_to_touch") or []
    if isinstance(raw_files, list):
        out["files_to_touch"] = [str(f).strip() for f in raw_files if str(f).strip()]

    raw_risks = d.get("risks") or []
    if isinstance(raw_risks, list):
        out["risks"] = [str(r).strip() for r in raw_risks if str(r).strip()]

    return out


# ── Plan storage + approval polling ───────────────────────────────────────────

async def save_plan(
    db: AsyncSession,
    run_id: int,
    plan: dict[str, Any],
) -> None:
    """Persist the plan JSON on the task_run row."""
    await db.execute(
        text("UPDATE task_runs SET plan_json = :plan WHERE id = :run_id"),
        {"plan": json.dumps(plan), "run_id": run_id},
    )
    await db.commit()


async def reset_approval(db: AsyncSession, task_id: int) -> None:
    """Clear any prior approval/rejection on the task before asking again."""
    await db.execute(
        text(
            "UPDATE tasks SET plan_approved_at = NULL, plan_rejected_at = NULL "
            "WHERE id = :id"
        ),
        {"id": task_id},
    )
    await db.commit()


async def wait_for_approval(
    db: AsyncSession,
    task_id: int,
    timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    poll_interval: int = POLL_INTERVAL_SECONDS,
) -> tuple[str, datetime | None]:
    """Poll tasks.plan_approved_at / plan_rejected_at until one is set.

    Returns ("approved" | "rejected" | "timeout", timestamp_or_None).
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds

    while True:
        result = await db.execute(
            text(
                "SELECT plan_approved_at, plan_rejected_at FROM tasks WHERE id = :id"
            ),
            {"id": task_id},
        )
        row = result.fetchone()
        if row:
            approved_at, rejected_at = row
            if approved_at is not None:
                return "approved", approved_at
            if rejected_at is not None:
                return "rejected", rejected_at

        if asyncio.get_event_loop().time() >= deadline:
            return "timeout", None

        await asyncio.sleep(poll_interval)


def render_plan_for_implementation(plan: dict[str, Any]) -> str:
    """Render the approved plan as a prompt block for the implementation phase."""
    if not plan:
        return ""

    lines = [
        "## Approved Implementation Plan (HUMAN-APPROVED CONTRACT)",
        "You previously produced this plan and a human reviewer approved it.",
        "You MUST implement exactly this plan. Do not expand scope, do not touch",
        "files not listed in files_to_touch.",
        "",
        f"Summary: {plan.get('summary', '')}",
        f"Approach: {plan.get('approach', '')}",
    ]

    steps = plan.get("steps") or []
    if steps:
        lines.append("")
        lines.append("Steps:")
        for s in steps:
            lines.append(f"  {s.get('n', '?')}. {s.get('desc', '')}")

    files = plan.get("files_to_touch") or []
    if files:
        lines.append("")
        lines.append("Files to touch (exhaustive allow-list):")
        for f in files:
            lines.append(f"  - {f}")

    risks = plan.get("risks") or []
    if risks:
        lines.append("")
        lines.append("Risks the reviewer is aware of:")
        for r in risks:
            lines.append(f"  - {r}")

    ack = plan.get("acceptance_check")
    if ack:
        lines.append("")
        lines.append(f"Acceptance check: {ack}")

    return "\n".join(lines)
