"""System-LLM powered helpers: task/plan generation and goal decomposition."""

import json as _json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from models.base import async_session
from services import (
    agent_backends,
    decomposer,
    llm_client,
    system_llm,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal")


async def _read_system_llm_settings() -> tuple[str, str, str]:
    async with async_session() as db:
        llm = await system_llm.read(db)
    return llm.vendor, llm.model, llm.mode


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
                return _json.loads(cleaned[start : end + 1])
            except _json.JSONDecodeError:
                pass
    snippet = (text_content or "").strip()[:300] or "(empty response)"
    raise ValueError(f"response was not valid JSON: {snippet}")


# ─── DevTask Skill ──────────────────────────────────────────────────────────


class GenerateTaskRequest(BaseModel):
    description: str


@router.post("/generate-task")
async def generate_task(body: GenerateTaskRequest):
    """Call the system LLM with devtask skill prompt to generate task JSON.

    Uses whichever vendor/model is configured in the ``system_llm_vendor``
    and ``system_llm_model`` settings (editable on the /settings page).
    Falls back to the registry's system-LLM default when unset.
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
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"System LLM error: {exc}") from exc

    try:
        task = _extract_json_object(text_content)
    except ValueError as exc:
        raise HTTPException(
            502, f"Failed to parse devtask response as JSON ({sys_vendor}) — {exc}"
        ) from exc

    # Enforce DevServer defaults regardless of what the system LLM emitted:
    # Billing → Max (subscription), Skip Verification → true, model → latest
    # Claude Opus. These only fill in missing/empty values so an explicit user
    # request carried through the skill prompt still wins.
    if not task.get("claude_mode"):
        task["claude_mode"] = "max"
    if task.get("skip_verify") is None:
        task["skip_verify"] = True
    if not task.get("claude_model"):
        task["claude_model"] = agent_backends.GENERATED_TASK_DEFAULT_MODEL

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
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"System LLM error: {exc}") from exc

    try:
        plan = _extract_json_object(text_content)
    except ValueError as exc:
        raise HTTPException(
            502, f"Failed to parse devplan response as JSON ({sys_vendor}) — {exc}"
        ) from exc

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
                # Best-effort mirror into the operator's notes; the plan is returned anyway.
                logger.warning("Could not write plan to %s", file_path, exc_info=True)

    return plan


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
        raise HTTPException(
            404 if "not found" in (result.get("reason") or "") else 502,
            result.get("reason") or "expand failed",
        )
    return result


@router.post("/goals/{node_id}/rollup")
async def rollup_goal(node_id: int):
    """Synthesise completed children into the parent's summary + 0–100 score."""
    async with async_session() as db:
        result = await decomposer.rollup_node(db, node_id)
    if not result.get("ok"):
        raise HTTPException(409, result.get("reason") or "rollup not ready")
    return result
