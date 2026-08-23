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
import contextlib
import queue as _queue
import subprocess
import sys
import threading
from typing import AsyncIterator, Callable

__all__ = ["run", "run_shell_streamed", "run_streamed", "ProcessRunner"]

# ── Streaming defaults ──────────────────────────────────────────────────────
# A single ``--output-format stream-json`` line from an agent CLI carries a
# whole tool_result, which for a ``Read`` of a large file is megabytes. We do
# our own newline splitting over ``read(n)`` chunks rather than
# ``StreamReader.readline()`` precisely so that a long line cannot raise
# ``ValueError: Separator is not found, and chunk exceed the limit`` (the 64 KiB
# default) and kill the stream mid-conversation. ``line_limit`` is only a
# safety valve: a "line" that grows past it is flushed as-is instead of being
# buffered forever by a process that never emits a newline.
_DEFAULT_LINE_LIMIT = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024
# Bounded so a stalled consumer applies backpressure all the way down: the
# queue fills, the pump stops draining the pipe, and the child blocks on write
# instead of the worker ballooning in memory.
_QUEUE_MAXSIZE = 256


async def _pump_stream(
    reader: asyncio.StreamReader,
    tag: str,
    out: asyncio.Queue,
    line_limit: int,
) -> None:
    """Split ``reader`` into lines and put ``(tag, line)`` onto ``out``."""
    buf = bytearray()
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(buf[: idx + 1])
            del buf[: idx + 1]
            await out.put((tag, line.decode(errors="replace")))
        if len(buf) > line_limit:
            await out.put((tag, bytes(buf).decode(errors="replace")))
            buf.clear()
    if buf:
        await out.put((tag, bytes(buf).decode(errors="replace")))


async def _pumps_sentinel(pumps: list[asyncio.Task], out: asyncio.Queue) -> None:
    """Wait for every pump, then push the ``None`` end-of-output sentinel."""
    try:
        await asyncio.gather(*pumps)
    finally:
        await out.put(None)


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

    @abc.abstractmethod
    def run_streamed(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin_input: bytes | None = None,
        line_limit: int = _DEFAULT_LINE_LIMIT,
    ) -> AsyncIterator[tuple[str, str]]:
        """Run ``cmd``, yielding ``(channel, line)`` as output arrives.

        ``channel`` is ``'stdout'`` or ``'stderr'``; the final yield is
        ``('exit', str(returncode))``. Unlike
        :meth:`run_shell_streamed` this takes an argv list (no shell quoting),
        keeps **stderr on its own channel** (a CLI warning must never be
        interleaved into an NDJSON stdout stream), and accepts ``env`` — all
        three are prerequisites for driving an agent CLI in
        ``--output-format stream-json`` mode.

        Implementations must kill the child when the consumer closes the
        generator (``GeneratorExit`` / cancellation), otherwise an abandoned
        browser tab leaks an agent process holding a repo directory.
        """


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

    async def run_streamed(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin_input: bytes | None = None,
        line_limit: int = _DEFAULT_LINE_LIMIT,
    ) -> AsyncIterator[tuple[str, str]]:
        """Native-asyncio streaming exec. See :meth:`ProcessRunner.run_streamed`."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=(
                asyncio.subprocess.PIPE
                if stdin_input is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=max(line_limit, _READ_CHUNK),
        )
        out: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        assert proc.stdout is not None and proc.stderr is not None
        pumps = [
            asyncio.create_task(_pump_stream(proc.stdout, "stdout", out, line_limit)),
            asyncio.create_task(_pump_stream(proc.stderr, "stderr", out, line_limit)),
        ]
        sentinel = asyncio.create_task(_pumps_sentinel(pumps, out))

        if stdin_input is not None and proc.stdin is not None:
            proc.stdin.write(stdin_input)
            with contextlib.suppress(Exception):
                await proc.stdin.drain()
            proc.stdin.close()

        try:
            # asyncio.timeout(None) is a valid no-op, so one code path covers both.
            async with asyncio.timeout(timeout):
                while True:
                    item = await out.get()
                    if item is None:
                        break
                    yield item
                rc = await proc.wait()
            yield ("exit", str(rc))
        finally:
            # Reached on normal exit, on timeout, and — critically — when the
            # consumer abandons the generator: GeneratorExit lands here and the
            # child must not survive it.
            for task in (sentinel, *pumps):
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(sentinel, *pumps, return_exceptions=True)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()


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

    async def run_streamed(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin_input: bytes | None = None,
        line_limit: int = _DEFAULT_LINE_LIMIT,
    ) -> AsyncIterator[tuple[str, str]]:
        # An async generator does nothing until the first ``__anext__``, so the
        # SelectorEventLoop's NotImplementedError surfaces here rather than at
        # construction — hence the manual first-step probe.
        native = super().run_streamed(
            cmd,
            cwd=cwd,
            env=env,
            timeout=timeout,
            stdin_input=stdin_input,
            line_limit=line_limit,
        )
        try:
            first = await native.__anext__()
        except StopAsyncIteration:
            return
        except NotImplementedError:
            with contextlib.suppress(Exception):
                await native.aclose()
            async for item in self._run_streamed_thread(
                cmd, cwd, env, timeout, stdin_input, line_limit
            ):
                yield item
            return
        try:
            yield first
            async for item in native:
                yield item
        finally:
            with contextlib.suppress(Exception):
                await native.aclose()

    async def _run_streamed_thread(
        self,
        cmd: list[str],
        cwd: str | None,
        env: dict | None,
        timeout: float | None,
        stdin_input: bytes | None,
        line_limit: int,
    ) -> AsyncIterator[tuple[str, str]]:
        """Thread-based streaming fallback for the SelectorEventLoop.

        One reader thread per pipe feeds a plain ``queue.Queue``; the event loop
        drains it through the default executor. ``line_limit`` is unused here —
        ``Popen`` pipes iterate by line with no separator cap.
        """
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        q: _queue.Queue = _queue.Queue(maxsize=_QUEUE_MAXSIZE)
        alive = {"n": 2}
        lock = threading.Lock()

        def reader(pipe, tag: str) -> None:
            try:
                for raw in iter(pipe.readline, b""):
                    q.put((tag, raw.decode(errors="replace")))
            finally:
                with lock:
                    alive["n"] -= 1
                    if alive["n"] == 0:
                        q.put(None)

        threads = [
            threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True),
        ]
        for t in threads:
            t.start()
        if stdin_input is not None and proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.write(stdin_input)
                proc.stdin.close()

        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    item = await loop.run_in_executor(None, q.get)
                    if item is None:
                        break
                    yield item
                rc = await loop.run_in_executor(None, proc.wait)
            yield ("exit", str(rc or 0))
        finally:
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()

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


def run_streamed(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: float | None = None,
    stdin_input: bytes | None = None,
    line_limit: int = _DEFAULT_LINE_LIMIT,
) -> AsyncIterator[tuple[str, str]]:
    """Run ``cmd`` via the platform runner, yielding output lines as they arrive.

    Yields ``('stdout' | 'stderr', line)`` and finally ``('exit', str(rc))``.
    Raises ``asyncio.TimeoutError`` on timeout. This is the transport behind the
    Ask Agent panel: it keeps stderr separate so an agent CLI's NDJSON stdout
    stays parseable, and it kills the child as soon as the consumer stops
    iterating.

    Note this is a plain ``def`` returning an async generator, so nothing runs
    until the first ``async for`` step.
    """
    return _RUNNER.run_streamed(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        stdin_input=stdin_input,
        line_limit=line_limit,
    )
