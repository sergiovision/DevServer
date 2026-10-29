"""Every subprocess is bounded: cancellation and timeouts kill the child."""

import asyncio
import os
import sys

import pytest

from services import git_ops, proc

_SPAWN_GRANDCHILD = (
    "import subprocess, sys, time; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    "print(child.pid, flush=True); time.sleep(30)"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
async def test_cancellation_kills_the_whole_process_group(tmp_path):
    pid_file = tmp_path / "pids"
    script = _SPAWN_GRANDCHILD.replace(
        "print(child.pid, flush=True)", f"open({str(pid_file)!r}, 'w').write(str(child.pid))"
    )
    running = asyncio.create_task(proc.run([sys.executable, "-c", script], timeout=60))
    async with asyncio.timeout(10):
        while not pid_file.exists() or not pid_file.read_text():
            await asyncio.sleep(0.05)
    grandchild = int(pid_file.read_text())

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    async with asyncio.timeout(5):
        while _alive(grandchild):
            await asyncio.sleep(0.05)


async def test_timeout_raises_and_kills():
    with pytest.raises(TimeoutError):
        await proc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)


async def test_git_run_requires_a_timeout():
    with pytest.raises(TypeError):
        await git_ops._run(["git", "--version"])  # type: ignore[call-arg]


async def test_git_timeout_surfaces_as_git_timeout_error(monkeypatch):
    async def slow_run(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(git_ops.proc_util, "run", slow_run)
    with pytest.raises(git_ops.GitTimeoutError):
        await git_ops._run(["git", "fetch"], timeout=1)
