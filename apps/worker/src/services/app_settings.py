"""Small async helpers for the key/value ``settings`` table.

The table stores each value as JSONB (e.g. ``'"glm"'``, ``'2'``, ``'false'``).
Depending on the DB driver, ``SELECT value`` may come back already parsed
(asyncpg) or as a JSON string (psycopg), so these helpers normalise both and
coerce to the type the caller wants.

Free-level module — no pro dependency — so both editions can read config the
same way (mirrors the inline pattern in ``services/compaction.py``).
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def get_setting(session: AsyncSession, key: str, default=None):
    """Return the parsed value for ``key``, or ``default`` if unset."""
    try:
        row = await session.execute(
            text("SELECT value FROM settings WHERE key = :k"), {"k": key}
        )
        v = row.scalar_one_or_none()
    except Exception:
        logger.debug("settings read failed for %r", key)
        return default
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


async def get_int_setting(session: AsyncSession, key: str, default: int) -> int:
    """Return ``key`` coerced to int, or ``default`` on miss/parse failure."""
    v = await get_setting(session, key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def get_bool_setting(session: AsyncSession, key: str, default: bool) -> bool:
    """Return ``key`` coerced to bool, or ``default`` on miss."""
    v = await get_setting(session, key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default
