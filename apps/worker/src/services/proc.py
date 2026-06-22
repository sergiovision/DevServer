"""Subprocess helper, split by platform into separate runner classes.

Every git / agent / CLI invocation in the worker goes through the module-level
:func:`run` / :func:`run_shell_streamed` functions. Those delegate to a single
:class:`ProcessRunner` instance chosen **once, at import time**, from the host
platform:

- **macOS / Linux** → :class:`PosixProcessRunner`. This is the original,
  proven native-``asyncio`` transport, verbatim. There is no Windows code on
  this path at all — no thread fallback, no ``NotImplementedError`` handling —
  so the POSIX behaviour is exactly what it was before Windows support landed.

- **Windows** → :class:`WindowsProcessRunner`. ``asyncio.create_subprocess_*``
  only works on the ``ProactorEventLoop``; uvicorn, when started with
  ``--reload`` or ``--workers > 1``, deliberately runs on a
  ``SelectorEventLoop`` (built via a ``loop_factory`` that bypasses the
  event-loop *policy*, so forcing the Proactor policy in startup code has no
  effect). Spawning a subprocess there raises ``NotImplementedError``. The
  Windows runner catches that and transparently falls back to running blocking
  ``subprocess`` calls on a worker thread. Every CLI call the worker makes is
  fully awaited anyway, so there is no benefit lost in the thread path.

All four concrete platforms return ``(returncode, stdout_bytes, stderr_bytes)``
and callers decode as they already do. Timeouts raise ``asyncio.TimeoutError``
(the thread path's ``subprocess.TimeoutExpired`` is translated) and a missing
binary raises ``FileNotFoundError`` — identical across platforms — so existing
``except`` blocks at the call sites keep working unchanged.
"""

import abc
import asyncio
import subprocess
import sys
import threading
from typing import Callable

__all__ = ["run", "run_shell_streamed", "ProcessRunner"]


class ProcessRunner(abc.ABC):
    """Platform-specific subprocess transport.

    Subclasses implement the two primitives the worker needs: a fully-awaited
    ``run`` (collect all output) and a streaming ``run_shell_streamed`` (line
    callbacks). The public module functions delegate to a single selected
    instance — call sites never see the platform split.
    """

    @abc.abstractmethod
    async def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin_devnull: bool = False,
        stderr_devnull: bool = False,
        stdin_input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run ``cmd`` to completion; return ``(returncode, stdout, stderr)``."""

    @abc.abstractmethod
    async def run_shell_streamed(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        on_line: Callable[[str], None],
    ) -> int:
        """Run a *shell* command, streaming merged stdout+stderr line-by-line."""


class PosixProcessRunner(ProcessRunner):
    """Native ``asyncio`` subprocess transport for macOS and Linux.

    This is the original implementation that predates Windows support. The
    default event loop on every POSIX platform supports
    ``create_subprocess_exec`` / ``create_subprocess_shell``, so there is no
    fallback to consider here — keeping this path pristine is the whole point
    of the platform split.
    """

    async def run(
        self,
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

        Raises ``asyncio.TimeoutError`` on timeout (the process is killed
        first) and ``FileNotFoundError`` if the binary is missing.

        ``stdin_input`` feeds bytes to the process's stdin (then closes it) and
        takes precedence over ``stdin_devnull``.
        """
        stderr_pipe = (
            asyncio.subprocess.DEVNULL if stderr_devnull else asyncio.subprocess.PIPE
        )
        if stdin_input is not None:
            stdin_mode: int | None = asyncio.subprocess.PIPE
        elif stdin_devnull:
            stdin_mode = asyncio.subprocess.DEVNULL
        else:
            stdin_mode = None

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=stdin_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_pipe,
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

    async def run_shell_streamed(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        on_line: Callable[[str], None],
    ) -> int:
        """Run a *shell* command, streaming merged stdout+stderr line-by-line.

        ``on_line(text)`` is invoked for every output line as it arrives.
        Returns the exit code. Raises ``asyncio.TimeoutError`` on timeout — the
        process is killed first.
        """
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

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


class WindowsProcessRunner(PosixProcessRunner):
    """Windows-native runner: native transport when available, else a thread.

    Inherits the native implementation from :class:`PosixProcessRunner` and
    wraps each call so that a ``NotImplementedError`` (raised when the worker
    runs on a ``SelectorEventLoop`` — uvicorn ``--reload`` / multi-worker on
    win32) transparently falls back to a blocking ``subprocess`` call on a
    worker thread. None of this code is reachable on macOS / Linux.
    """

    async def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin_devnull: bool = False,
        stderr_devnull: bool = False,
        stdin_input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        try:
            return await super().run(
                cmd,
                cwd=cwd,
                env=env,
                timeout=timeout,
                stdin_devnull=stdin_devnull,
                stderr_devnull=stderr_devnull,
                stdin_input=stdin_input,
            )
        except NotImplementedError:
            return await self._run_in_thread(
                cmd, cwd, env, timeout, stdin_devnull, stderr_devnull, stdin_input
            )

    async def run_shell_streamed(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        on_line: Callable[[str], None],
    ) -> int:
        try:
            return await super().run_shell_streamed(
                cmd, cwd=cwd, timeout=timeout, on_line=on_line
            )
        except NotImplementedError:
            try:
                return await asyncio.to_thread(
                    self._shell_streamed_sync, cmd, cwd, timeout, on_line
                )
            except subprocess.TimeoutExpired:
                raise asyncio.TimeoutError from None

    # ── Blocking fallbacks, run on a worker thread ──────────────────────
    @staticmethod
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
        self,
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
                self._sync_run,
                cmd, cwd, env, timeout, stdin_devnull, stderr_devnull, stdin_input,
            )
        except subprocess.TimeoutExpired:
            # Mirror the native path so callers' `except asyncio.TimeoutError` works.
            raise asyncio.TimeoutError from None
        return cp.returncode or 0, cp.stdout or b"", cp.stderr or b""

    @staticmethod
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


# ── Platform selection (once, at import) ────────────────────────────────────
_RUNNER: ProcessRunner = (
    WindowsProcessRunner() if sys.platform == "win32" else PosixProcessRunner()
)


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
    """Run ``cmd`` to completion via the platform runner.

    Returns ``(returncode, stdout_bytes, stderr_bytes)``. Raises
    ``asyncio.TimeoutError`` on timeout and ``FileNotFoundError`` if the binary
    is missing. ``stdin_input`` feeds bytes to stdin and takes precedence over
    ``stdin_devnull`` — this is how a large prompt is delivered to the agent
    CLI on Windows, where passing it as an argv element would blow cmd.exe's
    ~8191-char command-line limit via the npm ``.CMD`` shim.
    """
    return await _RUNNER.run(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        stdin_devnull=stdin_devnull,
        stderr_devnull=stderr_devnull,
        stdin_input=stdin_input,
    )


async def run_shell_streamed(
    cmd: str,
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    on_line: Callable[[str], None],
) -> int:
    """Run a *shell* command via the platform runner, streaming output lines.

    ``on_line(text)`` is invoked for every output line as it arrives. Returns
    the exit code. Raises ``asyncio.TimeoutError`` on timeout — the process is
    killed first. On the Windows thread fallback ``on_line`` is invoked from a
    worker thread (plain synchronous I/O only — do not touch the event loop).
    """
    return await _RUNNER.run_shell_streamed(
        cmd, cwd=cwd, timeout=timeout, on_line=on_line
    )
