"""DB-free tests for the per-repo memory knowledge base and the agent lanes."""

from __future__ import annotations

import pathlib

import pytest

from routes import memory_kb
from services import corpus, memory, memory_facts, repo_kb
from services.runner import agent_call, prompt
from services.runner.prompt import AGENT_LANES_BLOCK

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


# ── Transcript chunking ─────────────────────────────────────────────────────


def test_chunk_text_overlap_and_coverage():
    text = "".join(chr(65 + (i % 26)) for i in range(5000))
    chunks = repo_kb.chunk_text(text, size=2000, overlap=256)
    assert [len(c) for c in chunks] == [2000, 2000, 1512]
    assert chunks[0][-256:] == chunks[1][:256]
    assert chunks[1][-256:] == chunks[2][:256]
    assert chunks[0] + chunks[1][256:] + chunks[2][256:] == text


def test_chunk_text_edge_cases():
    assert repo_kb.chunk_text("") == []
    assert repo_kb.chunk_text("   ") == []
    assert repo_kb.chunk_text("hello") == ["hello"]
    assert repo_kb.chunk_text("abcd", size=4, overlap=1) == ["abcd"]


# ── Rerank parsing ──────────────────────────────────────────────────────────


def test_parse_rerank_indices_filters_and_dedupes():
    assert repo_kb.parse_rerank_indices("3, 0,5", 6) == [3, 0, 5]
    assert repo_kb.parse_rerank_indices("1,1,9,-2,x,2", 3) == [1, 2]
    assert repo_kb.parse_rerank_indices("none of them", 3) == []


# ── Rendering ───────────────────────────────────────────────────────────────


def test_render_wake_digest_full():
    digest = repo_kb.render_wake_digest(
        repo_name="demo",
        status_counts={"done": 3, "failed": 1, "running": 1},
        topics=["apps", "docs"],
        facts=[{"subject": "tests", "predicate": "run with", "object": "pytest"}],
        decisions=[{"choice": "httpx", "reasoning": "async"}, {"choice": ""}],
        dead_facts=[("db", "is", "sqlite")],
    )
    assert "Repo: demo — 5 tasks (3 done, 1 failed, 75% success)." in digest
    assert "Active areas: apps, docs." in digest
    assert "- tests run with pytest" in digest
    assert "- chose httpx because async" in digest
    assert "No longer true" in digest and "- db is sqlite" in digest


def test_render_wake_digest_empty_without_knowledge():
    assert (
        repo_kb.render_wake_digest(
            repo_name="demo",
            status_counts={"done": 2},
            topics=[],
            facts=[],
            decisions=[],
            dead_facts=[],
        )
        == ""
    )


def test_render_facts():
    assert memory_facts.render_facts([]) == ""
    block = memory_facts.render_facts([{"subject": "a", "predicate": "b", "object": "c"}])
    assert block.startswith("## Known Facts") and "- a b c" in block


def test_render_decision_and_recall_summary():
    md = {"problem": "p", "choice": "X", "alternatives": ["Y", "Z"], "reasoning": "fast"}
    content = memory.render_decision(md)
    assert content.splitlines()[0] == "Decision: X"
    assert "Alternatives considered: Y; Z" in content
    rendered = memory.render_memory_recall(
        [{"memory_type": "decision", "similarity": 0.5, "metadata": md, "content": content}]
    )
    assert "- [decision sim=0.50] chose X because fast (over Y, Z)" in rendered


def test_no_inline_vector_cast_in_sql():
    """``:emb::vector`` does not bind under SQLAlchemy ``text()``; use CAST."""
    for name in ("memory.py", "memory_facts.py", "repo_kb.py", "corpus.py"):
        assert ":emb::vector" not in (SRC / "services" / name).read_text(), name


# ── Corpus file selection ───────────────────────────────────────────────────


def test_corpus_classify():
    assert corpus._classify("a.py", "code") == "python"
    assert corpus._classify("a.tsx", "code") == "typescript"
    assert corpus._classify("README.md", "code") is None
    assert corpus._classify("README.md", "doc") == "markdown"
    assert corpus._classify("LICENSE", "doc") == "text"
    assert corpus._classify("a.py", "doc") is None


def test_corpus_iter_files_skips_vendored(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "empty.py").write_text("")
    (tmp_path / "node_modules" / "x" / "b.js").write_text("var b;\n")
    files = [(rel, lang) for _, rel, lang in corpus._iter_files(str(tmp_path), "code")]
    assert files == [("src/a.py", "python")]


def test_corpus_repo_root_local_and_remote(tmp_path):
    assert corpus.repo_root("local", str(tmp_path), "demo") == str(tmp_path)
    assert corpus.repo_root("local", "", "demo") is None
    assert corpus.repo_root("gitea", "", "demo").endswith("demo")


@pytest.mark.asyncio
async def test_corpus_ingest_without_root_is_not_ok():
    result = await corpus.ingest(None, 1, "/definitely/not/here", kind="doc")  # type: ignore[arg-type]
    assert result == {"ok": False, "kind": "doc", "error": "repo root not available"}


# ── Repo map drill-down ─────────────────────────────────────────────────────


def test_safe_subdir_guards():
    from services.repo_map import _safe_subdir

    assert _safe_subdir("/tmp/wt", "apps/worker") == "apps/worker"
    assert _safe_subdir("/tmp/wt", "/etc") is None
    assert _safe_subdir("/tmp/wt", "../etc") is None
    assert _safe_subdir("/tmp/wt", "a/../../etc") is None
    assert _safe_subdir("/tmp/wt", None) is None


async def test_repo_map_lane(tmp_path):
    (tmp_path / "apps/worker").mkdir(parents=True)
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "apps/worker/a.py").write_text("def foo():\n    pass\n")
    (tmp_path / "apps/web/b.py").write_text("def bar():\n    pass\n")

    sub = await memory_kb._repo_map(str(tmp_path), "apps/worker", 8000)
    assert sub["ok"] and "a.py" in sub["map"] and "b.py" not in sub["map"]
    assert (await memory_kb._repo_map(None, None, 8000))["ok"] is False


# ── Route helpers ───────────────────────────────────────────────────────────


def test_path_matching_and_remote_normalisation(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    assert memory_kb._path_matches(str(root), str(root))
    assert memory_kb._path_matches(str(root / "pkg"), str(root))
    assert not memory_kb._path_matches(str(tmp_path / "repo2"), str(root))
    assert not memory_kb._path_matches(str(root), None)
    assert memory_kb._norm_remote("https://Git.example/Org/Repo.git/") == (
        "https://git.example/org/repo"
    )


def test_router_exposes_lanes():
    paths = {(m, r.path) for r in memory_kb.router.routes for m in r.methods}  # type: ignore[attr-defined]
    expected = {
        ("POST", "/internal/tasks/{task_key}/decisions"),
        ("POST", "/internal/tasks/{task_key}/facts"),
        ("GET", "/internal/tasks/{task_key}/memory/recall"),
        ("POST", "/internal/tasks/{task_key}/memory/remember"),
        ("GET", "/internal/tasks/{task_key}/memory/transcripts"),
        ("GET", "/internal/tasks/{task_key}/memory/facts"),
        ("GET", "/internal/tasks/{task_key}/memory/decisions"),
        ("GET", "/internal/tasks/{task_key}/repo-map"),
        ("GET", "/internal/tasks/{task_key}/corpus/search"),
        ("POST", "/internal/facts/{fact_id}/invalidate"),
        ("GET", "/internal/repos/resolve"),
        ("GET", "/internal/repos/{repo_id}/wake-digest"),
        ("GET", "/internal/repos/{repo_id}/facts"),
        ("POST", "/internal/repos/{repo_id}/facts"),
        ("POST", "/internal/repos/{repo_id}/decisions"),
        ("POST", "/internal/repos/{repo_id}/memory/remember"),
        ("GET", "/internal/repos/{repo_id}/corpus/stats"),
        ("POST", "/internal/repos/{repo_id}/corpus/ingest"),
        ("GET", "/internal/corpus/search"),
    }
    assert expected <= paths


# ── Agent lanes: prompt + env ───────────────────────────────────────────────


def test_lanes_block_lists_every_lane():
    block = "\n".join(AGENT_LANES_BLOCK)
    for lane in (
        "messages/inbox",
        "messages/send",
        "memory/recall",
        "memory/transcripts",
        "memory/facts",
        "memory/decisions",
        "memory/remember",
        "corpus/search",
        "repo-map",
        "/decisions",
        "/facts",
    ):
        assert f"$DEVSERVER_TASK_KEY/{lane.lstrip('/')}" in block, lane
    assert prompt._LANE_BASE == "$DEVSERVER_WORKER_URL/internal/tasks/$DEVSERVER_TASK_KEY"


def test_lane_env(monkeypatch):
    monkeypatch.setattr(agent_call.settings, "worker_host", "0.0.0.0")
    monkeypatch.setattr(agent_call.settings, "worker_port", 8123)
    assert agent_call.lane_env(None, None) is None
    env = agent_call.lane_env({"PATH": "/bin"}, "DEMO-1")
    assert env == {
        "PATH": "/bin",
        "DEVSERVER_WORKER_URL": "http://127.0.0.1:8123",
        "DEVSERVER_TASK_KEY": "DEMO-1",
    }
    # An operator-set URL (e.g. a worker behind a proxy) is kept.
    env = agent_call.lane_env({"DEVSERVER_WORKER_URL": "http://w:1"}, "K")
    assert env is not None and env["DEVSERVER_WORKER_URL"] == "http://w:1"
