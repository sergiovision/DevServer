"""Repository resource: git refresh, architecture diagram, external import."""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from models.base import async_session
from models.repo import Repo
from services import (
    git_ops,
    import_sources,
    repo_map,
)

router = APIRouter(prefix="/internal")


# ─── Refresh Git ───────────────────────────────────────────────────────────


@router.post("/repos/{repo_id}/refresh-git")
async def refresh_git(repo_id: int):
    """Clone or fetch a repo's bare repo and worktree."""
    async with async_session() as db:
        repo = await db.get(Repo, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")

    if git_ops.is_local_provider(getattr(repo, "provider", None)):
        # Local-folder repo — nothing to clone or fetch, just validate the
        # Local Root Folder (stored in repos.gitea_url) is a git checkout.
        result = await git_ops.refresh_local_repo(repo.gitea_url)
    else:
        result = await git_ops.refresh_repo(
            repo_name=repo.name,
            clone_url=repo.clone_url,
            default_branch=repo.default_branch,
            gitea_token=repo.gitea_token or None,
            provider=getattr(repo, "provider", None),
        )
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.get("/repos/{repo_id}/diagram")
async def repo_diagram(repo_id: int):
    """Mermaid architecture diagram (module tree) for a repo.

    Deterministic, no LLM — built on the free ``repo_map`` module. Resolves the
    repo's on-disk root (the Local Root Folder for local repos, else the task
    worktree) and walks the top directory levels.
    """
    async with async_session() as db:
        repo = await db.get(Repo, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")

    if git_ops.is_local_provider(getattr(repo, "provider", None)):
        try:
            root = git_ops.resolve_local_root(repo.gitea_url)
        except RuntimeError:
            root = None
    else:
        root = git_ops.get_worktree_path(repo.name)

    if not root or not os.path.isdir(root):
        raise HTTPException(
            status_code=409,
            detail="repo worktree not available — run Refresh Git first",
        )

    mermaid, stats = repo_map.build_mermaid(root)
    return {"repo_id": repo_id, "mermaid": mermaid, "stats": stats}


# ─── External import (Confluence) ────────────────────────────────────────────
# Read-only source access: list sources/scopes, search, fetch + convert to
# Markdown drafts. Target creation (tasks/ideas), enqueueing and the
# ``external_imports`` idempotency history live in the Next.js API layer —
# the worker never writes import targets.


class ImportFetchRequest(BaseModel):
    """Batch draft fetch — the web import route resolves these into targets."""

    ids: list[str]
    repo_id: int | None = None


async def _import_repo(repo_id: int | None) -> Repo | None:
    """Load the repo whose per-repo Confluence overrides should apply."""
    if repo_id is None:
        return None
    async with async_session() as db:
        res = await db.execute(select(Repo).where(Repo.id == repo_id))
        return res.scalar_one_or_none()


def _import_source_or_404(name: str):
    src = import_sources.get_source(name)
    if src is None:
        raise HTTPException(404, f"unknown import source {name!r}")
    return src


@router.get("/import/sources")
async def import_list_sources(repo_id: int | None = None):
    """UI source selector: every registered source + whether it's usable."""
    repo = await _import_repo(repo_id)
    return [
        {"id": src.name, "label": src.label, "configured": src.is_configured(repo)}
        for src in import_sources.SOURCES.values()
    ]


@router.get("/import/confluence/ping")
async def import_confluence_ping(repo_id: int | None = None):
    src = _import_source_or_404("confluence")
    repo = await _import_repo(repo_id)
    return await src.ping(repo=repo)


@router.get("/import/confluence/scopes")
async def import_confluence_scopes(repo_id: int | None = None):
    src = _import_source_or_404("confluence")
    repo = await _import_repo(repo_id)
    try:
        return {"scopes": await src.list_scopes(repo=repo)}
    except Exception as exc:
        raise HTTPException(502, f"Confluence scopes failed: {exc}") from exc


@router.get("/import/confluence/search")
async def import_confluence_search(
    q: str = "",
    scope: str | None = None,
    limit: int = 25,
    repo_id: int | None = None,
):
    src = _import_source_or_404("confluence")
    repo = await _import_repo(repo_id)
    try:
        return {"items": await src.search(q, scope=scope, limit=limit, repo=repo)}
    except Exception as exc:
        raise HTTPException(502, f"Confluence search failed: {exc}") from exc


@router.get("/import/confluence/item/{external_id}")
async def import_confluence_item(external_id: str, repo_id: int | None = None):
    """Fetch one page and return its normalised draft (Markdown preview)."""
    src = _import_source_or_404("confluence")
    repo = await _import_repo(repo_id)
    try:
        item = await src.get(external_id, repo=repo)
    except Exception as exc:
        raise HTTPException(502, f"Confluence fetch failed: {exc}") from exc
    return src.to_draft(item).to_dict()


@router.post("/import/confluence/import")
async def import_confluence_batch(body: ImportFetchRequest):
    """Batch-fetch drafts for the selected pages. Pure fetch/convert — the
    caller (Next.js) inserts targets, enqueues, and records history."""
    src = _import_source_or_404("confluence")
    repo = await _import_repo(body.repo_id)
    drafts: list[dict] = []
    errors: list[dict] = []
    for external_id in body.ids[:50]:
        try:
            item = await src.get(external_id, repo=repo)
            drafts.append(src.to_draft(item).to_dict())
        except Exception as exc:
            errors.append({"external_id": external_id, "error": str(exc)})
    return {"drafts": drafts, "errors": errors}
