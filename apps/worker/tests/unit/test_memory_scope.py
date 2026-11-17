"""DB-free tests: recall scoping, digest success rate, rerank fallback."""

from __future__ import annotations

from types import SimpleNamespace

from services import memory, repo_kb


def test_scope_clause_single_repo_excludes_transcripts():
    where, params = memory._scope_clause(1, memory_type=None, topic=None, exclude_repo_id=None)
    assert "repo_id = :repo_id" in where and "memory_type <> 'transcript'" in where
    assert "archived_at IS NULL AND invalidated_at IS NULL" in where
    assert params == {"repo_id": 1}


def test_scope_clause_tunnel_and_explicit_type():
    where, params = memory._scope_clause(
        None, memory_type="transcript", topic="api", exclude_repo_id=7
    )
    assert "repo_id <> :xrepo AND repo_id IS NOT NULL" in where
    assert "memory_type = :mtype" in where and "topic = :topic" in where
    assert params == {"xrepo": 7, "mtype": "transcript", "topic": "api"}


def test_scope_clause_all_repos():
    where, params = memory._scope_clause(None, memory_type=None, topic=None, exclude_repo_id=None)
    assert where.startswith("TRUE") and params == {}


def test_wake_digest_counts_test_status_as_success():
    digest = repo_kb.render_wake_digest(
        repo_name="demo",
        status_counts={"test": 3, "failed": 1},
        topics=["api"],
        facts=[],
        decisions=[],
        dead_facts=[],
    )
    assert "(3 done, 1 failed, 75% success)" in digest


async def test_llm_rerank_falls_back_to_fused_order(monkeypatch):
    from services import llm_client

    async def _boom(**_kw):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(llm_client, "complete", _boom)
    llm = SimpleNamespace(vendor="anthropic", model="m", mode="api")
    items = [{"content": str(i)} for i in range(5)]
    assert await repo_kb.llm_rerank(llm, "q", items, 2) == items[:2]


async def test_llm_rerank_uses_reply_order(monkeypatch):
    from services import llm_client

    async def _reply(**_kw):
        return "3, 1"

    monkeypatch.setattr(llm_client, "complete", _reply)
    llm = SimpleNamespace(vendor="anthropic", model="m", mode="api")
    items = [{"content": str(i)} for i in range(5)]
    assert await repo_kb.llm_rerank(llm, "q", items, 3) == [items[3], items[1], items[0]]
