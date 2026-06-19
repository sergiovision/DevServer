"""DB-free unit tests for the memory KB seam (Foundation B + Tier 1.1)."""

import pytest

from services.pro.repo_kb import _chunk_text
from services._free_hooks import FreeHooks, _NoopRepoMemory


def test_chunk_text_overlap_and_coverage():
    text = "".join(chr(65 + (i % 26)) for i in range(5000))
    chunks = _chunk_text(text, size=2000, overlap=256)
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [2000, 2000, 1512]
    # adjacent chunks share `overlap` chars
    assert chunks[0][-256:] == chunks[1][:256]
    assert chunks[1][-256:] == chunks[2][:256]
    # reassembling by dropping the overlap reproduces the original
    rebuilt = chunks[0] + chunks[1][256:] + chunks[2][256:]
    assert rebuilt == text


def test_chunk_text_edge_cases():
    assert _chunk_text("") == []
    assert _chunk_text("   ") == []
    assert _chunk_text("hello") == ["hello"]
    assert _chunk_text("abcd", size=4, overlap=1) == ["abcd"]


@pytest.mark.asyncio
async def test_free_noop_repo_memory():
    kb = FreeHooks().repo_memory(None, 1)
    assert isinstance(kb, _NoopRepoMemory)
    assert await kb.recall("q") == []
    assert await kb.recall_iterative("q") == []
    assert await kb.recall_transcripts("q") == []
    assert await kb.recall_decisions("q") == []
    assert kb.render_recall([{"content": "x"}]) == ""
    assert await kb.remember("x") == 0
    assert await kb.archive_transcript(1) == 0
    assert await kb.predict_outcome("t", "d") is None
    assert await kb.get_wake_digest() == ""


# ── RLM E4 — repo-map drill-down ────────────────────────────────────────────

def test_no_inline_vector_cast_in_memory_sql():
    """Regression guard: `:emb::vector` does NOT bind under SQLAlchemy text()
    (the `(?!:)` param regex skips it), so vector recall silently fails. The
    SQL must use the CAST(:emb AS vector) form instead.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src/services/pro/memory.py"
    assert ":emb::vector" not in src.read_text(), \
        "use CAST(:emb AS vector); :emb::vector won't bind under SQLAlchemy text()"


def test_safe_subdir_guards():
    from services.repo_map import _safe_subdir

    assert _safe_subdir("/tmp/wt", "apps/worker") == "apps/worker"
    assert _safe_subdir("/tmp/wt", "/etc") is None          # absolute
    assert _safe_subdir("/tmp/wt", "../etc") is None         # traversal
    assert _safe_subdir("/tmp/wt", "a/../../etc") is None    # nested traversal
    assert _safe_subdir("/tmp/wt", None) is None
    assert _safe_subdir("/tmp/wt", "") is None


def test_build_repo_map_subdir_scopes(tmp_path):
    from services import repo_map

    (tmp_path / "apps/worker").mkdir(parents=True)
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "apps/worker/a.py").write_text("def foo():\n    pass\n")
    (tmp_path / "apps/web/b.py").write_text("def bar():\n    pass\n")

    full, _ = repo_map.build_repo_map(str(tmp_path))
    assert "a.py" in full and "b.py" in full

    sub, _ = repo_map.build_repo_map(str(tmp_path), subdir="apps/worker")
    assert "a.py" in sub and "b.py" not in sub

    # Traversal degrades to a global scan instead of crashing/escaping.
    esc, _ = repo_map.build_repo_map(str(tmp_path), subdir="../..")
    assert isinstance(esc, str)
