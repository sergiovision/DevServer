"""asyncpg pool lifecycle.

No jsonb codec is registered on purpose: the same pool is handed to PgQueuer,
whose models expect jsonb as text. Domain code passes ``json.dumps(...)`` with
an explicit ``::jsonb`` cast and decodes with :func:`json.loads`.
"""

from __future__ import annotations

import asyncpg

from reservations.config import Settings


async def create_pool(settings: Settings, *, max_size: int | None = None) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=max_size or settings.db_pool_max_size,
        command_timeout=60,
    )
    assert pool is not None
    return pool


async def close_pool(pool: asyncpg.Pool | None) -> None:
    if pool is not None:
        await pool.close()
