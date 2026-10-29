"""One retry helper instead of a hand-rolled loop per call site.

The worker retries in a few places — an agent CLI that hit a vendor 429, an
attempt that failed with a rate-limit error class, Telegram's single-consumer
409. Each used to carry its own schedule tuple and its own ``while`` loop.
:class:`Backoff` captures the schedule once and :func:`retry_async` captures
the loop, so a call site only states *what* to retry and *when*.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Backoff:
    """A delay schedule: ``delays[n]`` seconds before retry ``n`` (0-based).

    Retries beyond the schedule reuse its last delay. ``jitter`` adds up to
    that many random seconds so concurrent workers do not retry in lockstep.
    """

    delays: tuple[float, ...]
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if not self.delays:
            raise ValueError("Backoff needs at least one delay")

    def delay(self, retry_index: int) -> float:
        base = self.delays[min(retry_index, len(self.delays) - 1)]
        return base + random.uniform(0, self.jitter) if self.jitter else base


@dataclass(frozen=True)
class RetryNotice:
    """What :func:`retry_async` passes to ``on_retry`` before it sleeps."""

    retry_number: int  # 1-based
    max_retries: int
    sleep_seconds: float


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    backoff: Backoff,
    retry_on: Callable[[T], bool],
    on_retry: Callable[[RetryNotice], Awaitable[None]] | None = None,
) -> T:
    """Call ``fn`` up to ``attempts`` times while ``retry_on(result)`` is true.

    Returns the first result that should not be retried, or the last result
    once the attempts are used up — the caller decides what an exhausted
    retry means. Exceptions are never swallowed: they propagate on the spot.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    result = await fn()
    for retry_number in range(1, attempts):
        if not retry_on(result):
            return result
        sleep_seconds = backoff.delay(retry_number - 1)
        if on_retry is not None:
            await on_retry(RetryNotice(retry_number, attempts - 1, sleep_seconds))
        await asyncio.sleep(sleep_seconds)
        result = await fn()
    return result
