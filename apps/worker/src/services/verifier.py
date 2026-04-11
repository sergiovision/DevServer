"""Verification runner — executes pre/build/test/lint commands in the worktree."""

import asyncio
import logging
from typing import IO

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 600  # 10 minutes per step


async def _run_step(
    name: str,
    cmd: str,
    cwd: str,
    timeout: int = DEFAULT_TIMEOUT,
    log_file: IO | None = None,
) -> tuple[bool, str]:
    """Run a single verification step, streaming output to log_file in real-time."""
    if not cmd or not cmd.strip():
        return True, ""

    logger.info("Verify [%s]: %s", name, cmd)
    if log_file:
        log_file.write(f"\n$ {cmd}\n")
        log_file.flush()

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        output_chunks: list[str] = []

        async def _stream() -> None:
            assert proc.stdout
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace")
                output_chunks.append(text)
                if log_file:
                    log_file.write(text)
                    log_file.flush()

        try:
            await asyncio.wait_for(_stream(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Verify [%s] TIMEOUT after %ds", name, timeout)
            if log_file:
                log_file.write(f"\n[TIMEOUT after {timeout}s]\n")
                log_file.flush()
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            output = "".join(output_chunks)
            return False, f"{name} timed out after {timeout}s\n{output[-2000:]}"

        await proc.wait()
        output = "".join(output_chunks)

        if proc.returncode != 0:
            logger.error("Verify [%s] FAILED (exit %d)", name, proc.returncode)
            if log_file:
                log_file.write(f"\n[FAILED exit={proc.returncode}]\n")
                log_file.flush()
            return False, output
        logger.info("Verify [%s] PASSED", name)
        if log_file:
            log_file.write(f"[PASSED]\n")
            log_file.flush()
        return True, output

    except Exception as exc:
        logger.exception("Verify [%s] exception", name)
        if log_file:
            log_file.write(f"\n[EXCEPTION: {exc}]\n")
            log_file.flush()
        return False, str(exc)


async def run_verify(
    worktree_path: str,
    pre_cmd: str = "",
    build_cmd: str = "",
    test_cmd: str = "",
    lint_cmd: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    log_file: IO | None = None,
) -> tuple[bool, str]:
    """Run all verification steps in sequence, streaming output to log_file.

    Returns (success, error_output). error_output is empty on success.
    """
    steps = [
        ("pre", pre_cmd),
        ("build", build_cmd),
        ("test", test_cmd),
        ("lint", lint_cmd),
    ]

    if log_file:
        log_file.write(f"\n{'─'*60}\nVERIFICATION\n{'─'*60}\n")
        log_file.flush()

    for name, cmd in steps:
        ok, output = await _run_step(name, cmd, worktree_path, timeout, log_file)
        if not ok:
            return False, f"[{name}] failed:\n{output[-3000:]}"

    logger.info("All verification steps passed")
    return True, ""
