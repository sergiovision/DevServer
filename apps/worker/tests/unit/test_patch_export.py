"""``services.patch_export``: patch files for patch-mode tasks."""

import subprocess

import pytest

from config import settings
from services import patch_export


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo_with_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "log_dir", str(tmp_path / "logs"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "agent/demo-1")
    return repo


async def test_exports_one_patch_per_commit(repo_with_branch):
    for name in ("b", "c"):
        (repo_with_branch / f"{name}.txt").write_text(name)
        _git(repo_with_branch, "add", "-A")
        _git(repo_with_branch, "commit", "-q", "-m", f"add {name}")

    patches = await patch_export.export_patches(
        task_key="DEMO-1", repo_path=str(repo_with_branch), base="main"
    )

    assert patches.commits == 2
    assert [p.name for p in patch_export.list_patches("DEMO-1")] == list(patches.files)
    assert patches.files[0].endswith(".patch")


async def test_no_commits_exports_nothing(repo_with_branch):
    patches = await patch_export.export_patches(
        task_key="DEMO-1", repo_path=str(repo_with_branch), base="main"
    )
    assert patches == patch_export.PatchSet(commits=0, files=())
    assert patch_export.list_patches("DEMO-1") == []


@pytest.mark.parametrize(
    ("task_key", "file_name"),
    [
        ("..", "x.patch"),
        ("DEMO-1", "../../etc/passwd"),
        ("DEMO-1", "..%2Fsecret.patch"),
        ("DEMO-1", "notes.txt"),
        ("a/b", "x.patch"),
    ],
)
def test_traversal_attempts_are_rejected(tmp_path, monkeypatch, task_key, file_name):
    monkeypatch.setattr(settings, "log_dir", str(tmp_path))
    with pytest.raises(patch_export.InvalidPatchPath):
        patch_export.resolve_patch_file(task_key, file_name)
