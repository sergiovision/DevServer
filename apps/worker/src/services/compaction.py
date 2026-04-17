"""Context compaction — summarise a long task transcript via the system LLM.

When a single task spans many retries the conversation history on the
agent side keeps growing. Every retry re-feeds that history into the
vendor's context window which (a) burns tokens, and (b) is the primary
cause of the 30K-tokens/minute rate-limit failures observed in
production before Phase-1 hardening.

Compaction is the escape hatch: take every prior ``task_runs.claude_output``
plus ``error_log`` + the accumulated ``error_class_counts``, send them to
the configured system LLM with a focused "summarise what's been tried
and what remains" prompt, and save the result on ``tasks.compacted_context``.

On the next retry:

    1. ``tasks.compacted_context`` becomes the sole context block in the
       prompt (it replaces repo_map + memory_recall + reality_signal).
    2. ``session_id`` is cleared so the CLI starts a fresh conversation
       — the summary IS the history now.
    3. A ``context_compacted`` event is emitted so the dashboard marks
       the attempt and shows the summary inline.

The compaction call itself runs on the system LLM (GLM by default) to
keep the cost negligible. It never raises — on any failure we log and
return ``False`` and the caller falls through to the normal retry path.
"""

from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.setting import Setting
from services import llm_client

logger = logging.getLogger(__name__)


# Hard cap per-run output we send into the summariser. The LLM context
# is enough to chew through several retries; 20 KB per run keeps the total
# request bounded even when the task has already spent 5+ attempts.
_PER_RUN_OUTPUT_CAP = 20_000
_MAX_RUNS_INCLUDED = 6

_COMPACT_PROMPT_TEMPLATE = """You are summarising an autonomous coding agent's work so \
far on a single task. The agent will pick up work from your summary alone — \
its chat history is being discarded. Be concrete, no fluff, no encouragement.

Task key:  {task_key}
Title:     {title}
Status:    {status} (attempt {next_attempt} about to start)
Repo:      {repo_name}

## Original description
{description}

## Acceptance criteria
{acceptance}

## Prior attempts ({n_runs} runs, newest first)
{runs_block}

## Recurring error classes
{error_class_block}

---

Produce a single markdown document with EXACTLY these sections:

1. **What has been tried** — bullet list, 1 line per attempt, what the agent \
did and what broke.
2. **Files already touched** — the files modified so far (best-effort from \
the transcripts).
3. **Known-dead approaches** — anything the transcripts prove doesn't work; \
the next attempt must not retry these.
4. **Next steps** — the smallest actionable todo-list that, if completed, \
would finish the task.
5. **Open questions** — anything ambiguous in the description or blocked \
on external state.

Stay under 1500 words. No preamble, no sign-off."""


async def _read_system_llm(session: AsyncSession) -> tuple[str, str]:
    """Fetch (vendor, model) from the settings table. Defaults to GLM."""
    vendor_row = await session.execute(
        text("SELECT value FROM settings WHERE key = 'system_llm_vendor'")
    )
    model_row = await session.execute(
        text("SELECT value FROM settings WHERE key = 'system_llm_model'")
    )
    v = vendor_row.scalar_one_or_none()
    m = model_row.scalar_one_or_none()
    vendor = "glm"
    model = "glm-5.1"
    if v:
        try:
            vendor = _json.loads(v) if isinstance(v, str) and v.startswith('"') else str(v)
        except Exception:
            pass
    if m:
        try:
            model = _json.loads(m) if isinstance(m, str) and m.startswith('"') else str(m)
        except Exception:
            pass
    return vendor, model


async def compact_task(
    session: AsyncSession,
    *,
    task_id: int,
    reason: str = "manual",
) -> dict:
    """Summarise a task's transcript and write the result onto the task row.

    Returns a dict: ``{"ok": bool, "summary": str, "chars_in": int,
    "chars_out": int, "error": str | None}``.

    Side-effects:
        - UPDATE tasks SET compacted_context, compacted_at, compact_count+=1
        - INSERT into task_events (event_type='context_compacted')

    Does NOT clear session_id by itself — the caller (agent_runner)
    decides whether the next attempt should resume or start fresh.
    """
    # Load task + repo in one shot
    row = (await session.execute(
        text(
            """
            SELECT t.task_key, t.title, t.status, t.description, t.acceptance,
                   t.compact_count, r.name
            FROM tasks t
            LEFT JOIN repos r ON r.id = t.repo_id
            WHERE t.id = :tid
            """
        ),
        {"tid": task_id},
    )).fetchone()
    if not row:
        return {"ok": False, "summary": "", "chars_in": 0, "chars_out": 0,
                "error": f"task {task_id} not found"}

    task_key, title, status, description, acceptance, compact_count, repo_name = row

    # Collect the last N runs and stitch them in chronological order (newest first)
    run_rows = (await session.execute(
        text(
            """
            SELECT attempt, status, claude_output, error_log, duration_ms, turns, finished_at
            FROM task_runs
            WHERE task_id = :tid
            ORDER BY id DESC
            LIMIT :lim
            """
        ),
        {"tid": task_id, "lim": _MAX_RUNS_INCLUDED},
    )).fetchall()

    if not run_rows:
        return {"ok": False, "summary": "", "chars_in": 0, "chars_out": 0,
                "error": "no prior runs to compact"}

    run_blocks = []
    for attempt, run_status, claude_output, error_log, duration_ms, turns, finished_at in run_rows:
        out = (claude_output or "")[-_PER_RUN_OUTPUT_CAP:]
        err = (error_log or "")[:2000]
        when = finished_at.isoformat() if finished_at else "running"
        run_blocks.append(
            f"### Attempt {attempt} — {run_status} "
            f"(turns={turns}, {duration_ms}ms, finished={when})\n"
            f"Error: {err or '(none)'}\n"
            f"Output tail:\n{out}\n"
        )
    runs_block = "\n---\n".join(run_blocks)

    # Summarise recurring error classes from recent events
    ec_rows = (await session.execute(
        text(
            """
            SELECT payload->>'class' AS cls, COUNT(*) AS n
            FROM task_events
            WHERE task_id = :tid AND event_type = 'error_classified'
            GROUP BY payload->>'class'
            ORDER BY n DESC
            LIMIT 10
            """
        ),
        {"tid": task_id},
    )).fetchall()
    error_class_block = (
        "\n".join(f"- {c}: {n} hits" for c, n in ec_rows if c) or "(none observed)"
    )

    next_attempt = len(run_rows) + 1
    prompt = _COMPACT_PROMPT_TEMPLATE.format(
        task_key=task_key,
        title=title,
        status=status,
        next_attempt=next_attempt,
        repo_name=repo_name or "(unknown)",
        description=(description or "(none)")[:2000],
        acceptance=(acceptance or "(none)")[:2000],
        n_runs=len(run_rows),
        runs_block=runs_block,
        error_class_block=error_class_block,
    )

    chars_in = len(prompt)
    vendor, model = await _read_system_llm(session)

    try:
        summary = await llm_client.complete(
            vendor=vendor,
            model=model,
            prompt=prompt,
            max_tokens=3072,
            timeout=120,
        )
    except Exception as exc:
        logger.exception("compaction LLM call failed for task %s", task_key)
        await session.execute(
            text(
                "INSERT INTO task_events (task_id, event_type, payload) "
                "VALUES (:tid, 'context_compacted', CAST(:pl AS JSONB))"
            ),
            {"tid": task_id, "pl": _json.dumps({
                "ok": False, "reason": reason, "error": str(exc)[:300],
                "vendor": vendor, "model": model,
            })},
        )
        await session.commit()
        return {"ok": False, "summary": "", "chars_in": chars_in,
                "chars_out": 0, "error": str(exc)}

    summary = (summary or "").strip()
    if not summary:
        return {"ok": False, "summary": "", "chars_in": chars_in,
                "chars_out": 0, "error": "empty summary from LLM"}

    chars_out = len(summary)

    # Persist
    await session.execute(
        text(
            """
            UPDATE tasks
            SET compacted_context = :s,
                compacted_at = NOW(),
                compact_count = COALESCE(compact_count, 0) + 1,
                updated_at = NOW()
            WHERE id = :tid
            """
        ),
        {"s": summary, "tid": task_id},
    )
    await session.execute(
        text(
            "INSERT INTO task_events (task_id, event_type, payload) "
            "VALUES (:tid, 'context_compacted', CAST(:pl AS JSONB))"
        ),
        {"tid": task_id, "pl": _json.dumps({
            "ok": True,
            "reason": reason,
            "chars_in": chars_in,
            "chars_out": chars_out,
            "compression_ratio": round(chars_out / chars_in, 3) if chars_in else None,
            "vendor": vendor,
            "model": model,
            "runs_summarised": len(run_rows),
            "compact_count": (compact_count or 0) + 1,
        })},
    )
    await session.commit()

    logger.info(
        "Compacted task %s: %d chars -> %d chars (ratio %.2f, vendor=%s)",
        task_key, chars_in, chars_out,
        chars_out / chars_in if chars_in else 0.0, vendor,
    )

    return {
        "ok": True,
        "summary": summary,
        "chars_in": chars_in,
        "chars_out": chars_out,
        "error": None,
    }


def build_compacted_prompt_block(summary: str) -> str:
    """Render a compacted summary into a drop-in prompt block.

    Used by agent_runner._build_prompt when ``tasks.compacted_context``
    is set. Returns empty string for empty input so the caller can skip
    the block entirely.
    """
    if not summary or not summary.strip():
        return ""
    return (
        "## Compacted Prior Work\n"
        "The previous attempts on this task have been summarised. Treat this as \n"
        "the complete history — do not re-do any 'known-dead approach' listed below.\n\n"
        f"{summary.strip()}\n"
    )
