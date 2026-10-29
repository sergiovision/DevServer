"""Patch Export: downloadable ``.patch`` files for ``git_flow='patch'`` tasks.

A patch-mode task never pushes. After it succeeds, its commits are exported
with ``git format-patch`` into ``<log_dir>/patches/<task_key>/`` so the
operator can review them or apply them elsewhere with ``git am``.

File names arrive from HTTP requests, so every path is validated twice: the
names must match a strict pattern, and the resolved path must stay inside the
task's patch directory.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from config import settings
from services import git_ops

_TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATCH_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.patch$")


class InvalidPatchPath(ValueError):
    """A task key or file name that could escape the patch directory."""


@dataclass(frozen=True)
class PatchSet:
    commits: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class PatchFile:
    name: str
    size_bytes: int


def _patches_root() -> Path:
    return (Path(settings.log_dir) / "patches").resolve()


def is_safe_task_key(task_key: str) -> bool:
    """True when ``task_key`` can be used as a single path segment."""
    return bool(_TASK_KEY.fullmatch(task_key)) and task_key not in (".", "..")


def patch_dir(task_key: str) -> Path:
    if not is_safe_task_key(task_key):
        raise InvalidPatchPath(f"invalid task key {task_key!r}")
    root = _patches_root()
    directory = (root / task_key).resolve()
    if not directory.is_relative_to(root) or directory == root:
        raise InvalidPatchPath(f"invalid task key {task_key!r}")
    return directory


def resolve_patch_file(task_key: str, file_name: str) -> Path:
    """The on-disk path of one exported patch, or :class:`InvalidPatchPath`."""
    if not _PATCH_FILE.fullmatch(file_name):
        raise InvalidPatchPath(f"invalid patch file name {file_name!r}")
    directory = patch_dir(task_key)
    path = (directory / file_name).resolve()
    if path.parent != directory:
        raise InvalidPatchPath(f"invalid patch file name {file_name!r}")
    return path


async def export_patches(*, task_key: str, repo_path: str, base: str) -> PatchSet:
    """Replace the task's patch directory with ``base..HEAD`` as patch files."""
    directory = patch_dir(task_key)
    if directory.exists():
        shutil.rmtree(directory)
    commits = await git_ops.count_commits(repo_path, base=base)
    if commits == 0:
        return PatchSet(commits=0, files=())
    directory.mkdir(parents=True)
    written = await git_ops.format_patch(repo_path, base=base, out_dir=str(directory))
    return PatchSet(commits=commits, files=tuple(Path(path).name for path in written))


def list_patches(task_key: str) -> list[PatchFile]:
    directory = patch_dir(task_key)
    if not directory.is_dir():
        return []
    return [
        PatchFile(name=path.name, size_bytes=path.stat().st_size)
        for path in sorted(directory.glob("*.patch"))
    ]
