"""The code/doc corpus and temporal facts against a real PostgreSQL schema.

Ingest (symlinks, secret redaction, incremental re-index, pruning, truncation),
``path_prefix`` filtering, and fact search / concurrent supersession.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import text

from models.base import async_session
from services import corpus, embeddings, memory_facts

from .support import seed_task

pytestmark = pytest.mark.usefixtures("db_engine", "quiet_world")

_SECRET = "ghp_" + "a" * 36


async def _repo_id(git_repo: Path) -> int:
    task_id = await seed_task(git_repo)
    async with async_session() as db:
        return (
            await db.execute(text("SELECT repo_id FROM tasks WHERE id = :t"), {"t": task_id})
        ).scalar_one()


async def _paths(repo_id: int) -> set[str]:
    async with async_session() as db:
        rows = await db.execute(
            text("SELECT DISTINCT path FROM corpus_chunks WHERE repo_id = :r AND kind = 'code'"),
            {"r": repo_id},
        )
        return {r[0] for r in rows}


async def _ingest(repo_id: int, root: Path) -> dict:
    async with async_session() as db:
        return await corpus.ingest(db, repo_id, str(root), kind="code")


async def test_ingest_skips_symlinks_and_redacts(git_repo: Path, tmp_path: Path) -> None:
    repo_id = await _repo_id(git_repo)
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = 1\n")
    (git_repo / "linked.py").symlink_to(outside)
    (git_repo / "app.py").write_text(f'TOKEN = "{_SECRET}"\ndef retry_backoff():\n    pass\n')

    res = await _ingest(repo_id, git_repo)

    assert res["ok"] and res["files_indexed"] == 1
    assert await _paths(repo_id) == {"app.py"}
    async with async_session() as db:
        content = (
            await db.execute(text("SELECT content FROM corpus_chunks WHERE path = 'app.py'"))
        ).scalar_one()
    assert _SECRET not in content and "[REDACTED:github_pat]" in content


async def test_incremental_reindex_and_prune(git_repo: Path) -> None:
    repo_id = await _repo_id(git_repo)
    (git_repo / "a.py").write_text("A = 1\n")
    (git_repo / "b.py").write_text("B = 1\n")
    assert (await _ingest(repo_id, git_repo))["files_indexed"] == 2

    (git_repo / "a.py").write_text("A = 2\n")
    (git_repo / "b.py").unlink()
    res = await _ingest(repo_id, git_repo)

    assert res["files_indexed"] == 1 and res["files_pruned"] == 1
    assert await _paths(repo_id) == {"a.py"}


async def test_truncated_walk_prunes_only_missing_files(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_id = await _repo_id(git_repo)
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (git_repo / name).write_text(f"{name[0].upper()} = 1\n")
    await _ingest(repo_id, git_repo)
    (git_repo / "d.py").unlink()

    monkeypatch.setattr(corpus, "_MAX_FILES", 1)
    res = await _ingest(repo_id, git_repo)

    assert res["truncated"]
    # Files the walk never reached are still on disk and must survive.
    assert await _paths(repo_id) == {"a.py", "b.py", "c.py"}


async def test_path_prefix_keeps_dot_dirs_and_escapes_wildcards(git_repo: Path) -> None:
    repo_id = await _repo_id(git_repo)
    for rel in (".github/ci.py", "a_b/x.py", "aXb/y.py"):
        (git_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / rel).write_text("def retry_backoff():\n    return 'retry backoff'\n")
    await _ingest(repo_id, git_repo)

    async def hits(prefix: str) -> set[str]:
        async with async_session() as db:
            found = await corpus.search(db, repo_id, "retry backoff", path_prefix=prefix)
        return {h["path"] for h in found}

    assert await hits(".github/") == {".github/ci.py"}
    assert await hits("./.github") == {".github/ci.py"}
    assert await hits("a_b") == {"a_b/x.py"}


async def test_auto_index_is_off_by_default(git_repo: Path) -> None:
    repo_id = await _repo_id(git_repo)
    async with async_session() as db:
        await db.execute(text("DELETE FROM settings WHERE key = 'corpus_auto_index'"))
        await db.commit()
    started = await corpus.auto_index(
        repo_id, provider="local", local_root=str(git_repo), name="demo"
    )
    assert started == []


# ── Facts ───────────────────────────────────────────────────────────────────


def _fake_vector(value: str) -> list[float]:
    vec = [0.0] * 768
    vec[int(hashlib.sha256(value.encode()).hexdigest(), 16) % 768] = 1.0
    return vec


async def test_search_facts_exact_scan_returns_current_facts(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _embed(value: str) -> list[float]:
        return _fake_vector(value)

    monkeypatch.setattr(embeddings, "embed", _embed)
    repo_id = await _repo_id(git_repo)
    async with async_session() as db:
        await memory_facts.record_fact(db, repo_id, "tests", "run with", "make test")
        await memory_facts.record_fact(db, repo_id, "tests", "run with", "pytest -q")
        await memory_facts.record_fact(db, repo_id, "entry", "is", "src/main.py")
        hits = await memory_facts.search_facts(db, repo_id, "tests run with pytest -q")
        index = (await db.execute(text("SELECT to_regclass('idx_facts_embedding')"))).scalar_one()

    assert index is None  # ivfflat on a tiny table can return zero rows
    assert hits[0]["object"] == "pytest -q"
    assert {h["object"] for h in hits} == {"pytest -q", "src/main.py"}


async def test_concurrent_fact_writes_leave_one_current(git_repo: Path) -> None:
    repo_id = await _repo_id(git_repo)

    async def write(obj: str) -> int:
        async with async_session() as db:
            return await memory_facts.record_fact(db, repo_id, "build", "uses", obj)

    ids = await asyncio.gather(*(write(f"tool-{i}") for i in range(6)))
    assert all(ids)
    async with async_session() as db:
        current = await memory_facts.query_facts(db, repo_id, subject="build")
    assert len(current) == 1


async def test_fact_writes_are_secret_redacted(git_repo: Path) -> None:
    repo_id = await _repo_id(git_repo)
    async with async_session() as db:
        await memory_facts.record_fact(db, repo_id, "api", "token is", _SECRET)
        facts = await memory_facts.query_facts(db, repo_id)
    assert facts[0]["object"] == "[REDACTED:github_pat]"
