"""Patch export — generate git patches from a task's agent branch.

This module implements **Option A** of the cross-repo propagation proposal:
produce ``git format-patch`` output that a human (or an external CI pipeline)
can apply to a production repo via ``git am``.

Design notes for future **Option E** refactor
----------------------------------------------
The goal of this module is to be the single source of truth for "turn a task
branch into a set of applyable patches", so that a future ``apply_patches()``
function added on top of Option E can consume exactly the same output.

The public API deliberately:

- **Separates generation from delivery.** ``generate_patches()`` writes files
  to disk and returns a ``PatchSet`` describing them. It does not HTTP, it
  does not git-am, it does not care about the production worktree. That's
  what makes it Option-E-compatible.

- **Uses a stable on-disk layout.** Patches live under
  ``{log_dir}/{task_key}.patches/`` with a deterministic structure:
  per-commit ``0001-*.patch`` files plus a single ``combined.mbox`` that is
  ``cat 0001-*.patch 0002-*.patch ...``. Option E can read the exact same
  files straight off disk.

- **Works against the bare repo, not the live worktree.** After a task
  finishes, ``agent_runner`` resets the worktree back to the default branch,
  but the ``agent/*`` branch still exists in the bare repository. We generate
  patches from the bare repo so we do not have to re-checkout anything, do
  not need a repo lock, and can regenerate on demand weeks after the task
  completed.

- **Never raises on git failures.** Every error becomes a ``PatchSet`` with
  ``ok=False`` and an ``error`` message. Upstream callers (the worker HTTP
  handlers, ``agent_runner``) can log the error without killing the task.

Public surface
--------------
- ``generate_patches(task_key, base_branch, branch_name)`` — the main entry
  point. Generates patches and returns a ``PatchSet``.
- ``list_patches(task_key)``                          — enumerate existing
  patches without regenerating.
- ``get_patch_path(task_key, filename)``              — safe path lookup for
  HTTP download handlers (rejects ``..`` traversal).
- ``delete_patches(task_key)``                        — wipe the patches
  directory (used before a fresh regenerate).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import settings

logger = logging.getLogger(__name__)

# Conservative cap — we do not want a runaway agent to fill the disk with a
# thousand-commit patch series. The retry loop normally produces 1–5 commits.
MAX_PATCHES_PER_TASK = 200

# Reject any filename requested over HTTP that doesn't look like a real
# format-patch output. This is the single line that protects the download
# endpoint from path traversal.
_SAFE_PATCH_NAME = re.compile(r"^[\w\-.]+\.(?:patch|mbox)$")


@dataclass
class PatchFile:
    """A single file on disk that can be downloaded."""

    filename: str
    size_bytes: int
    kind: str  # "patch" | "mbox"

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
        }


@dataclass
class PatchSet:
    """Result of a generate_patches() call."""

    ok: bool
    task_key: str
    directory: str | None = None
    base_branch: str = ""
    branch_name: str = ""
    files: list[PatchFile] = field(default_factory=list)
    commits: int = 0
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    generated_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "task_key": self.task_key,
            "directory": self.directory,
            "base_branch": self.base_branch,
            "branch_name": self.branch_name,
            "files": [f.to_dict() for f in self.files],
            "commits": self.commits,
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "generated_at": self.generated_at,
            "error": self.error,
        }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _bare_repo_dir(repo_name: str) -> str:
    """Path to the bare clone that backs every DevServer worktree."""
    return os.path.join(settings.bare_repo_dir, repo_name)


def _patches_dir(task_key: str) -> str:
    """Where a task's patches live on disk.

    We co-locate with the existing task log file at
    ``{log_dir}/{task_key}.log`` so operators find both in the same place.
    """
    # Sanitise the key the same way git_ops.setup_worktree does, so the
    # directory name is a safe filesystem token.
    safe = task_key.replace(" ", "-").replace("/", "-").strip("-")
    return os.path.join(settings.log_dir, f"{safe}.patches")


async def _git(args: list[str], cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git subcommand — returns (rc, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        return -1, "", str(exc)


async def _count_commits(
    bare_repo: str, base_branch: str, branch_name: str,
) -> int:
    rc, out, _ = await _git(
        ["rev-list", "--count", f"{base_branch}..{branch_name}"],
        cwd=bare_repo,
    )
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


async def _collect_diff_stats(
    bare_repo: str, base_branch: str, branch_name: str,
) -> tuple[int, int, int]:
    """Return (files_changed, insertions, deletions)."""
    rc, out, _ = await _git(
        ["diff", "--shortstat", f"{base_branch}...{branch_name}"],
        cwd=bare_repo,
    )
    if rc != 0 or not out.strip():
        return 0, 0, 0

    # Example: " 3 files changed, 42 insertions(+), 7 deletions(-)"
    files_changed = insertions = deletions = 0
    m = re.search(r"(\d+) files? changed", out)
    if m:
        files_changed = int(m.group(1))
    m = re.search(r"(\d+) insertions?\(\+\)", out)
    if m:
        insertions = int(m.group(1))
    m = re.search(r"(\d+) deletions?\(-\)", out)
    if m:
        deletions = int(m.group(1))
    return files_changed, insertions, deletions


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_patches(
    *,
    task_key: str,
    repo_name: str,
    base_branch: str,
    branch_name: str,
) -> PatchSet:
    """Run ``git format-patch`` and write the results under the log dir.

    Works against the bare repository so no live worktree checkout is
    required. Always returns a :class:`PatchSet` — inspect ``ok`` for
    success.
    """
    result = PatchSet(
        ok=False,
        task_key=task_key,
        base_branch=base_branch,
        branch_name=branch_name,
    )

    bare_repo = _bare_repo_dir(repo_name)
    if not os.path.isdir(bare_repo):
        result.error = f"bare repo not found at {bare_repo}"
        return result

    # Verify the branch actually exists in the bare repo before we do any
    # work. A missing branch is a normal case (first run, task still
    # pending) — return a structured "no commits yet" result.
    rc, _, err = await _git(
        ["rev-parse", "--verify", branch_name],
        cwd=bare_repo,
    )
    if rc != 0:
        result.error = f"branch {branch_name!r} not found in bare repo"
        return result

    # Count commits first — if there are none, there is nothing to format.
    commits_ahead = await _count_commits(bare_repo, base_branch, branch_name)
    if commits_ahead == 0:
        result.ok = True
        result.commits = 0
        result.generated_at = datetime.now(timezone.utc).isoformat()
        return result

    if commits_ahead > MAX_PATCHES_PER_TASK:
        result.error = (
            f"branch has {commits_ahead} commits ahead of {base_branch}, "
            f"exceeds MAX_PATCHES_PER_TASK={MAX_PATCHES_PER_TASK}"
        )
        return result

    # Fresh directory every time — the previous patches are stale by
    # definition once the agent produces new commits.
    out_dir = _patches_dir(task_key)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    # Write all patches as a single mbox via --stdout so there is exactly one
    # file on disk. ``git am < combined.mbox`` handles 1-to-N commits equally
    # well; individual per-commit files are not needed.
    # Capture raw bytes directly — _git() decodes to str which would corrupt
    # binary patch hunks on re-encode.
    combined_path = os.path.join(out_dir, "combined.mbox")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "format-patch", "--stdout",
            "--subject-prefix", f"DevServer {task_key}",
            f"{base_branch}..{branch_name}",
            cwd=bare_repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (asyncio.TimeoutError, OSError) as exc:
        result.error = f"git format-patch error: {exc}"
        return result

    if proc.returncode != 0:
        result.error = f"git format-patch failed: {raw_err.decode(errors='replace').strip()[:500]}"
        return result

    if not raw_out:
        result.error = "format-patch produced no output"
        return result

    try:
        with open(combined_path, "wb") as fh:
            fh.write(raw_out)
        combined_size = os.path.getsize(combined_path)
    except OSError as exc:
        result.error = f"failed to write combined.mbox: {exc}"
        return result

    patch_files: list[PatchFile] = [
        PatchFile(filename="combined.mbox", size_bytes=combined_size, kind="mbox"),
    ]

    files_changed, insertions, deletions = await _collect_diff_stats(
        bare_repo, base_branch, branch_name,
    )

    result.ok = True
    result.directory = out_dir
    result.files = patch_files
    result.commits = commits_ahead
    result.files_changed = files_changed
    result.insertions = insertions
    result.deletions = deletions
    result.generated_at = datetime.now(timezone.utc).isoformat()

    logger.info(
        "patch_ops: generated %d patches for %s (%d commits, +%d/-%d)",
        len(patch_files), task_key, commits_ahead, insertions, deletions,
    )
    return result


def list_patches(task_key: str) -> PatchSet:
    """Return the current on-disk patches without regenerating.

    Used by the HTTP handler that backs the UI list view — cheap and
    synchronous because there's no git involved.
    """
    result = PatchSet(ok=True, task_key=task_key)
    out_dir = _patches_dir(task_key)
    if not os.path.isdir(out_dir):
        return result

    result.directory = out_dir

    for name in sorted(os.listdir(out_dir)):
        if not (name.endswith(".patch") or name.endswith(".mbox")):
            continue
        full = os.path.join(out_dir, name)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        kind = "mbox" if name.endswith(".mbox") else "patch"
        result.files.append(PatchFile(filename=name, size_bytes=size, kind=kind))

    # Best-effort timestamp from the mbox (or the first patch file).
    try:
        anchor = next(
            (f for f in result.files if f.kind == "mbox"),
            result.files[0] if result.files else None,
        )
        if anchor:
            mtime = os.path.getmtime(os.path.join(out_dir, anchor.filename))
            result.generated_at = datetime.fromtimestamp(
                mtime, tz=timezone.utc,
            ).isoformat()
    except OSError:
        pass

    return result


def get_patch_path(task_key: str, filename: str) -> str | None:
    """Return the safe absolute path to a named patch file, or None.

    Rejects any filename that is not of the form produced by format-patch —
    this is the guard against ``../../../etc/passwd`` style traversal over
    the HTTP download endpoint.
    """
    if not _SAFE_PATCH_NAME.match(filename):
        return None
    out_dir = _patches_dir(task_key)
    candidate = os.path.normpath(os.path.join(out_dir, filename))
    # Ensure the candidate is still inside out_dir after normalisation.
    if not candidate.startswith(os.path.abspath(out_dir) + os.sep) and \
            candidate != os.path.abspath(out_dir):
        # normpath might return relative path if out_dir was relative.
        abs_out = os.path.abspath(out_dir)
        abs_candidate = os.path.abspath(candidate)
        if not abs_candidate.startswith(abs_out + os.sep):
            return None
        candidate = abs_candidate
    if not os.path.isfile(candidate):
        return None
    return candidate


def delete_patches(task_key: str) -> bool:
    """Wipe the patches directory for a task. Returns True if anything was deleted."""
    out_dir = _patches_dir(task_key)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
        return True
    return False
