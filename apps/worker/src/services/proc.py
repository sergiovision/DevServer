"""Subprocess helper that works on every asyncio event loop.

On Windows, ``asyncio.create_subprocess_exec`` only works on the
``ProactorEventLoop``. uvicorn, when started with ``--reload`` or
``--workers > 1`` (``use_subprocess=True``), deliberately runs the server on
a ``SelectorEventLoop`` on win32 — and it builds that loop via a
``loop_factory`` that bypasses the event-loop *policy*, so forcing the
Proactor policy in our own startup code has no effect. Spawning a git/CLI
subprocess on a Selector loop raises ``NotImplementedError``.

Every git/agent/CLI invocation in the worker is short enough that it is fully
awaited (``proc.communicate()``) anyway, so there is no real benefit to the
native async transport. This module spawns natively when the loop supports it
and otherwise transparently falls back to running the blocking
``subprocess.run`` on a worker thread — so the worker behaves identically
regardless of how uvicorn was launched.

Callers get back ``(returncode, stdout_bytes, stderr_bytes)`` and decode as
they already do. Timeouts raise ``asyncio.TimeoutError`` (the thread path's
``subprocess.TimeoutExpired`` is translated) and a missing binary raises
``FileNotFoundError`` — identical to the native path — so existing
``except`` blocks at the call sites keep working unchanged.
"""

import asyncio
import subprocess
import threading
from typing import Callable

__all__ = ["run", "run_shell_streamed"]


def _sync_run(
    cmd: list[str],
    cwd: str | None,
    env: dict | None,
    timeout: float | None,
    stdin_devnull: bool,
    stderr_devnull: bool,
    stdin_input: bytes | None,
) -> subprocess.CompletedProcess:
    kwargs: dict = {}
    if stdin_input is not None:
        kwargs["input"] = stdin_input
    elif stdin_devnull:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if stderr_devnull else subprocess.PIPE,
        timeout=timeout,
        check=False,
        **kwargs,
    )


async def _run_in_thread(
    cmd: list[str],
    cwd: str | None,
    env: dict | None,
    timeout: float | None,
    stdin_devnull: bool,
    stderr_devnull: bool,
    stdin_input: bytes | None,
) -> tuple[int, bytes, bytes]:
    try:
        cp = await asyncio.to_thread(
            _sync_run, cmd, cwd, env, timeout, stdin_devnull, stderr_devnull, stdin_input
        )
    except subprocess.TimeoutExpired:
        # Mirror the native path so callers' `except asyncio.TimeoutError` works.
        raise asyncio.TimeoutError from None
    return cp.returncode or 0, cp.stdout or b"", cp.stderr or b""


async def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: float | None = None,
    stdin_devnull: bool = False,
    stderr_devnull: bool = False,
    stdin_input: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run ``cmd`` to completion; return ``(returncode, stdout, stderr)`` bytes.

    Uses the native asyncio subprocess transport when the running loop
    supports it, otherwise falls back to a thread (Windows Selector loop).
    Raises ``asyncio.TimeoutError`` on timeout and ``FileNotFoundError`` if
    the binary is missing.

    ``stdin_input`` feeds bytes to the process's stdin (then closes it). This
    is how a large prompt is delivered to the agent CLI on Windows, where
    passing it as an argv element would blow cmd.exe's ~8191-char command-line
    limit via the npm ``.CMD`` shim. ``stdin_input`` takes precedence over
    ``stdin_devnull``.
    """
    stderr_pipe = asyncio.subprocess.DEVNULL if stderr_devnull else asyncio.subprocess.PIPE
    if stdin_input is not None:
        stdin_mode = asyncio.subprocess.PIPE
    elif stdin_devnull:
        stdin_mode = asyncio.subprocess.DEVNULL
    else:
        stdin_mode = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=stdin_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_pipe,
        )
    except NotImplementedError:
        return await _run_in_thread(
            cmd, cwd, env, timeout, stdin_devnull, stderr_devnull, stdin_input
        )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_input), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout or b"", stderr or b""


def _shell_streamed_sync(
    cmd: str,
    cwd: str | None,
    timeout: float | None,
    on_line: Callable[[str], None],
) -> int:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    timed_out = {"v": False}

    def _kill() -> None:
        timed_out["v"] = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, _kill) if timeout else None
    if timer:
        timer.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            on_line(raw.decode(errors="replace"))
        proc.wait()
    finally:
        if timer:
            timer.cancel()

    if timed_out["v"]:
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return proc.returncode or 0


async def run_shell_streamed(
    cmd: str,
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    on_line: Callable[[str], None],
) -> int:
    """Run a *shell* command, streaming merged stdout+stderr line-by-line.

    ``on_line(text)`` is invoked for every output line as it arrives.
    Returns the exit code. Raises ``asyncio.TimeoutError`` on timeout — the
    process is killed first. Falls back to a thread when the running loop
    cannot spawn subprocesses (Windows Selector loop); in that case
    ``on_line`` is invoked from the worker thread (plain synchronous I/O
    only — do not touch the event loop from it).
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except NotImplementedError:
        try:
            return await asyncio.to_thread(
                _shell_streamed_sync, cmd, cwd, timeout, on_line
            )
        except subprocess.TimeoutExpired:
            raise asyncio.TimeoutError from None

    async def _stream() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            on_line(line.decode(errors="replace"))

    try:
        await asyncio.wait_for(_stream(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise
    await proc.wait()
    return proc.returncode or 0
