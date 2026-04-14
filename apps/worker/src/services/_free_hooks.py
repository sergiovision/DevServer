"""Free-tier hooks — no-op stubs for all pro features.

When the ``services/pro/`` folder is absent (public MIT repo), the
agent runner falls back to this module. Every method is a no-op or
returns a neutral default so the free version compiles and runs
without errors.

This file ships in BOTH repos (pro and free). The pro repo also has
``services/pro/__init__.py`` which provides the real implementations
via the same interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _SyntheticPreflightResult:
    """Minimal preflight result — always passes."""
    ok: bool = True
    has_hard_failure: bool = False
    violations: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    hint: str = ""


class FreeHooks:
    """No-op implementations of every pro hook.

    The agent runner does ``pro.some_method(...)`` everywhere. In free
    mode, this class is ``pro``, and every call silently succeeds with
    a neutral return value.
    """

    # ── Reality gate ────────────────────────────────────────────────
    async def run_reality_gate(self, **kwargs: Any) -> tuple[dict, str]:
        """Returns (empty_signal_dict, empty_prompt_text)."""
        return {}, ""

    # ── Memory ──────────────────────────────────────────────────────
    async def search_memory(self, **kwargs: Any) -> list[dict]:
        return []

    def render_memory_recall(self, memories: list[dict]) -> str:
        return ""

    async def store_memory(self, **kwargs: Any) -> None:
        pass

    # ── Plan gate ───────────────────────────────────────────────────
    async def run_plan_gate(self, **kwargs: Any) -> str:
        """Returns empty string = no approved plan (skip the gate)."""
        return ""

    async def get_preflight_allowlist(self, **kwargs: Any) -> list[str] | None:
        """Returns None = no allow-list enforcement."""
        return None

    # ── PR preflight ────────────────────────────────────────────────
    async def run_preflight(self, **kwargs: Any) -> _SyntheticPreflightResult:
        """Returns a synthetic pass — no checks in free mode."""
        return _SyntheticPreflightResult()

    def summarise_preflight(self, result: Any) -> dict:
        return {"ok": True, "files_changed": 0, "violations_by_kind": {}, "violations": [], "stats": {}}

    # ── Patch export ────────────────────────────────────────────────
    async def generate_patches(self, **kwargs: Any) -> None:
        pass

    # ── Budget circuit breaker ──────────────────────────────────────
    def check_budget(self, **kwargs: Any) -> tuple[str, str]:
        """Always returns ("ok", "") — no budget enforcement in free."""
        return "ok", ""

    # ── Pro Telegram (no-op stubs) ─────────────────────────────────
    # Free tier uses basic tg_send() in agent_runner. Pro tier calls
    # these methods for rich formatting, inline keyboards, and digests.
    async def tg_send_task_start(self, **kwargs: Any) -> None:
        pass

    async def tg_send_task_success(self, **kwargs: Any) -> None:
        pass

    async def tg_send_task_failed(self, **kwargs: Any) -> None:
        pass

    async def tg_send_plan_approval(self, **kwargs: Any) -> None:
        pass

    async def tg_send_vendor_failover(self, **kwargs: Any) -> None:
        pass

    async def tg_send_budget_warning(self, **kwargs: Any) -> None:
        pass

    async def tg_send_budget_exceeded(self, **kwargs: Any) -> None:
        pass

    async def tg_send_preflight_blocked(self, **kwargs: Any) -> None:
        pass

    async def tg_send_daily_digest(self, **kwargs: Any) -> str:
        return ""
