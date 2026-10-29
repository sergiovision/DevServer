"""Which LLM the worker itself uses for its own reasoning chores.

Task generation, transcript compaction, goal decomposition and the Ask Agent
assistant all call a "system LLM" chosen on the settings page. This module is
the one reader of those three settings; it used to be copied into four
modules with slightly different parsing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent_backends import SYSTEM_LLM_DEFAULT_MODEL, SYSTEM_LLM_DEFAULT_VENDOR

_KEYS = ("system_llm_vendor", "system_llm_model", "system_llm_mode")


@dataclass(frozen=True)
class SystemLLM:
    vendor: str = SYSTEM_LLM_DEFAULT_VENDOR
    model: str = SYSTEM_LLM_DEFAULT_MODEL
    #: 'max' = subscription through the vendor CLI, 'api' = direct HTTP.
    mode: str = "max"


def _as_text(value: object) -> str:
    """Settings hold JSON values; older rows store a JSON-encoded string."""
    if isinstance(value, str) and value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return str(decoded)
    return str(value)


async def read(db: AsyncSession) -> SystemLLM:
    rows = await db.execute(
        text("SELECT key, value FROM settings WHERE key = ANY(:keys)"), {"keys": list(_KEYS)}
    )
    found = {row.key: _as_text(row.value) for row in rows if row.value not in (None, "")}
    defaults = SystemLLM()
    return SystemLLM(
        vendor=found.get("system_llm_vendor") or defaults.vendor,
        model=found.get("system_llm_model") or defaults.model,
        mode=found.get("system_llm_mode") or defaults.mode,
    )
