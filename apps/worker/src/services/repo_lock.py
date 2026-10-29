"""Per-repository mutual exclusion, stored in the ``repo_locks`` table.

Only one task may work in a repository checkout at a time. A lock row names
its owner (the task key) and an expiry. While the owner is alive a heartbeat
keeps pushing the expiry forward, so:

- a crashed worker's lock simply expires and the repo becomes free again;
- stale-task recovery can tell a live task (fresh lock) from a dead one;
- releasing a lock only ever deletes the caller's own row, never someone
  else's.

This module is the only place that writes to ``repo_locks``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.base import async_session

logger = logging.getLogger(__name__)

#: How long a lock survives without a heartbeat.
LOCK_TTL = timedelta(minutes=5)
#: How often a held lock is renewed. Well inside the TTL so one missed beat
#: (a slow DB round-trip) never lets the lock lapse.
HEARTBEAT_INTERVAL_SECONDS = 60.0


class RepoLockBusy(Exception):
    """The repository is locked by another task; try again later."""

    def __init__(self, repo_name: str) -> None:
        super().__init__(f"repository {repo_name!r} is locked by another task")
        self.repo_name = repo_name


async def try_acquire(db: AsyncSession, *, repo_name: str, owner: str) -> bool:
    """Take the lock for ``owner`` if it is free (or expired). Commits."""
    await db.execute(
        text("DELETE FROM repo_locks WHERE repo_name = :repo AND expires_at < NOW()"),
        {"repo": repo_name},
    )
    inserted = await db.execute(
        text(
            """
            INSERT INTO repo_locks (repo_name, task_key, acquired_at, expires_at)
            VALUES (:repo, :owner, NOW(), NOW() + CAST(:ttl AS interval))
            ON CONFLICT (repo_name) DO NOTHING
            RETURNING repo_name
            """
        ),
        {"repo": repo_name, "owner": owner, "ttl": LOCK_TTL},
    )
    await db.commit()
    return inserted.first() is not None


async def renew(db: AsyncSession, *, repo_name: str, owner: str) -> bool:
    """Push the expiry of ``owner``'s lock forward. False if it is not held."""
    renewed = await db.execute(
        text(
            "UPDATE repo_locks SET expires_at = NOW() + CAST(:ttl AS interval) "
            "WHERE repo_name = :repo AND task_key = :owner RETURNING repo_name"
        ),
        {"repo": repo_name, "owner": owner, "ttl": LOCK_TTL},
    )
    await db.commit()
    return renewed.first() is not None


async def release(db: AsyncSession, *, repo_name: str, owner: str) -> None:
    """Delete ``owner``'s lock on ``repo_name``. Does not commit."""
    await db.execute(
        text("DELETE FROM repo_locks WHERE repo_name = :repo AND task_key = :owner"),
        {"repo": repo_name, "owner": owner},
    )


async def release_all_owned_by(db: AsyncSession, *, owner: str) -> None:
    """Delete every lock ``owner`` holds, whatever the repo. Does not commit."""
    await db.execute(text("DELETE FROM repo_locks WHERE task_key = :owner"), {"owner": owner})


async def _heartbeat(repo_name: str, owner: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        async with async_session() as db:
            if not await renew(db, repo_name=repo_name, owner=owner):
                logger.warning("Lock on %s is no longer held by %s", repo_name, owner)


@contextlib.asynccontextmanager
async def hold(repo_name: str, *, owner: str) -> AsyncIterator[None]:
    """Hold the repo lock for the duration of the block, renewing it.

    Raises :class:`RepoLockBusy` when another owner holds the lock.
    """
    async with async_session() as db:
        if not await try_acquire(db, repo_name=repo_name, owner=owner):
            raise RepoLockBusy(repo_name)

    heartbeat = asyncio.create_task(_heartbeat(repo_name, owner), name=f"lock:{repo_name}")
    try:
        yield
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        async with async_session() as db:
            await release(db, repo_name=repo_name, owner=owner)
            await db.commit()
