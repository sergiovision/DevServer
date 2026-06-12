"""Skill loader/registry — SKILL.md folders with progressive disclosure.

A skill is a folder under the repo-root ``skills/`` directory (overridable via
the ``SKILLS_DIR`` env var) containing a ``SKILL.md`` file:

    ---
    name: my-skill
    description: One-line summary used for progressive disclosure + the picker.
    domain: <optional — matches a project domain, or omit for generic>
    version: 1
    ---
    <markdown instructions…>

Progressive disclosure (the Anthropic Agent Skills pattern): :func:`list_skills`
returns only the frontmatter (name + description + domain), which is cheap and
safe to always load; :func:`load_skill` reads the full body, used only when a
task actually invokes the skill. :func:`render_skill_prompt_block` wraps a
skill's body for injection into an agent prompt.

We parse the frontmatter with a tiny hand-rolled reader (``key: value`` lines)
so the worker gains no new dependency on a YAML library.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _skills_dir() -> Path:
    """Resolve the skills directory. Defaults to ``<repo-root>/skills``."""
    env = os.environ.get("SKILLS_DIR")
    if env:
        return Path(env)
    # …/apps/worker/src/services/skills.py → parents[4] == repo root
    return Path(__file__).resolve().parents[4] / "skills"


@dataclass
class Skill:
    name: str
    description: str
    domain: str | None
    version: str
    path: str
    body: str = ""  # populated only by load_skill()


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into (frontmatter dict, body). Tolerant of no frontmatter."""
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.splitlines()
    # find the closing '---'
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, raw
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip().lower()] = val.strip()
    body = "\n".join(lines[end + 1:]).strip()
    return meta, body


def _read_skill(skill_md: Path, *, with_body: bool) -> Skill | None:
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except Exception:
        logger.exception("failed to read %s", skill_md)
        return None
    meta, body = _parse_frontmatter(raw)
    name = meta.get("name") or skill_md.parent.name
    return Skill(
        name=name,
        description=meta.get("description", ""),
        domain=meta.get("domain") or None,
        version=meta.get("version", "1"),
        path=str(skill_md),
        body=body if with_body else "",
    )


def discover(*, with_body: bool = False) -> list[Skill]:
    """Scan the skills directory for ``*/SKILL.md`` files."""
    root = _skills_dir()
    if not root.exists():
        return []
    out: list[Skill] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        s = _read_skill(skill_md, with_body=with_body)
        if s:
            out.append(s)
    return out


def list_skills() -> list[dict]:
    """Frontmatter-only listing (progressive disclosure)."""
    return [
        {"name": s.name, "description": s.description, "domain": s.domain,
         "version": s.version, "path": s.path}
        for s in discover(with_body=False)
    ]


def load_skill(name: str) -> Skill | None:
    """Full skill incl. body, by name."""
    for s in discover(with_body=True):
        if s.name == name:
            return s
    return None


def render_skill_prompt_block(skill: Skill) -> str:
    """Wrap a skill body for injection into an agent prompt."""
    return (
        f"## Skill: {skill.name}\n"
        f"_{skill.description}_\n\n"
        f"{skill.body}"
    )


async def sync_to_db(session: AsyncSession) -> dict:
    """Upsert discovered skills into the ``skills`` table (by name).

    Returns ``{"synced": [names], "count": n}``. Disk is the source of truth
    for name/description/domain/path/version; ``enabled`` is preserved.
    """
    found = discover(with_body=False)
    synced: list[str] = []
    for s in found:
        await session.execute(
            text(
                """
                INSERT INTO skills (name, description, path, domain, version)
                VALUES (:name, :desc, :path, :domain, :version)
                ON CONFLICT (name) DO UPDATE SET
                    description = EXCLUDED.description,
                    path        = EXCLUDED.path,
                    domain      = EXCLUDED.domain,
                    version     = EXCLUDED.version,
                    updated_at  = NOW()
                """
            ),
            {"name": s.name, "desc": s.description, "path": s.path,
             "domain": s.domain, "version": s.version},
        )
        synced.append(s.name)
    await session.commit()
    return {"synced": synced, "count": len(synced)}


async def get_skill_body_for_task(session: AsyncSession, task_id: int) -> str:
    """Return the rendered skill prompt block for a task's linked skill, or ''.

    Reads ``tasks.skill_id`` → ``skills.name`` via SQL (no ORM column needed),
    then loads the body from disk.
    """
    row = (await session.execute(
        text(
            "SELECT s.name FROM tasks t JOIN skills s ON s.id = t.skill_id "
            "WHERE t.id = :tid AND s.enabled"
        ),
        {"tid": task_id},
    )).fetchone()
    if not row:
        return ""
    skill = load_skill(row[0])
    return render_skill_prompt_block(skill) if skill else ""
