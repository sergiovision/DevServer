"""Free-tier hooks — free implementations and no-op stubs for pro features.

When the ``services/pro/`` folder is absent (public MIT repo), the
agent runner falls back to this module. A handful of hooks have real
free implementations (reality gate lite, PR preflight, budget circuit
breaker, basic hybrid memory recall); every other method is a no-op or
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

# Budget warn threshold — fraction of the cap at which a warning is emitted.
_BUDGET_WARN_THRESHOLD = 0.8


@dataclass
class _SyntheticPreflightResult:
    """Minimal preflight result — always passes."""
    ok: bool = True
    has_hard_failure: bool = False
    violations: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    hint: str = ""


class _NoopRepoMemory:
    """No-op per-repo Knowledge Base (mirrors services.pro.repo_kb.RepoMemory).

    Every method returns a neutral default so free-mode callers using
    ``pro.repo_memory(db, repo.id).recall(...)`` work without a pro install.
    """

    async def recall(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    async def recall_iterative(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    async def recall_transcripts(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    async def recall_decisions(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    def render_recall(self, memories: list[dict]) -> str:
        return ""

    async def archive_transcript(self, *args: Any, **kwargs: Any) -> int:
        return 0

    # ── Temporal facts (Tier 1.2) ──────────────────────────────────────
    async def record_fact(self, *args: Any, **kwargs: Any) -> int:
        return 0

    async def invalidate_fact(self, *args: Any, **kwargs: Any) -> bool:
        return False

    async def query_facts(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    async def timeline(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    async def search_facts(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    def render_facts(self, facts: list[dict]) -> str:
        return ""

    async def invalidate_memory(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def remember(self, *args: Any, **kwargs: Any) -> int:
        return 0

    async def record_decision(self, *args: Any, **kwargs: Any) -> int:
        return 0

    async def predict_outcome(self, *args: Any, **kwargs: Any) -> dict | None:
        return None

    async def get_wake_digest(self, *args: Any, **kwargs: Any) -> str:
        return ""

    async def set_wake_digest(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def wake_up_digest(self, *args: Any, **kwargs: Any) -> str:
        return ""

    async def build_wake_digest(self, *args: Any, **kwargs: Any) -> str:
        return ""


class _BasicRepoMemory(_NoopRepoMemory):
    """Per-repo memory with basic hybrid (vector + lexical RRF) recall.

    Overrides only store / recall / render; the advanced knowledge-base
    features (facts, decisions, transcripts, digests) stay no-ops.
    """

    def __init__(self, session: Any, repo_id: int) -> None:
        self.session = session
        self.repo_id = repo_id

    async def recall(self, query: str, *, limit: int = 5, **kwargs: Any) -> list[dict]:
        from services import memory
        return await memory.search_memory_hybrid(
            self.session, self.repo_id, query, limit=limit,
        )

    async def recall_iterative(self, query: str, *, limit: int = 5, **kwargs: Any) -> list[dict]:
        return await self.recall(query, limit=limit)

    def render_recall(self, memories: list[dict]) -> str:
        from services import memory
        return memory.render_memory_recall(memories)

    async def remember(
        self,
        content: str,
        *,
        kind: str = "experience",
        task_id: int | None = None,
        metadata: dict | None = None,
        topic: str | None = None,
        **kwargs: Any,
    ) -> int:
        from services import memory
        return await memory.store_memory(
            self.session,
            repo_id=self.repo_id,
            content=content,
            memory_type=kind,
            task_id=task_id,
            metadata=metadata,
            topic=topic,
        )


class FreeHooks:
    """Free implementations of the pro hook interface.

    The agent runner does ``pro.some_method(...)`` everywhere. In free
    mode, this class is ``pro``. Hooks with a free implementation do
    real work; the rest silently succeed with a neutral return value.
    """

    # ── Reality gate (lite: repo-map + recent-commit signals) ───────
    async def run_reality_gate(
        self,
        *,
        worktree_path: str = "",
        repo_map_text: str = "",
        title: str = "",
        description: str = "",
        acceptance: str = "",
        **kwargs: Any,
    ) -> tuple[dict, str]:
        """Run the lite reality gate and return (signal_dict, rendered_text)."""
        from services import reality_gate
        signal = await reality_gate.run_reality_gate(
            worktree_path=worktree_path,
            repo_map_text=repo_map_text,
            title=title,
            description=description,
            acceptance=acceptance,
        )
        return signal, reality_gate.render_for_prompt(signal)

    # ── Memory (basic hybrid recall) ────────────────────────────────
    def repo_memory(self, session: Any = None, repo_id: int | None = None, *args: Any, **kwargs: Any) -> "_NoopRepoMemory":
        """Per-repo memory object — basic hybrid recall when scoped to a repo."""
        if session is None or repo_id is None:
            return _NoopRepoMemory()
        return _BasicRepoMemory(session, repo_id)

    async def search_memory(
        self,
        *,
        session: Any,
        repo_id: int,
        query: str,
        limit: int = 3,
        **kwargs: Any,
    ) -> list[dict]:
        from services import memory
        return await memory.search_memory_hybrid(session, repo_id, query, limit=limit)

    def render_memory_recall(self, memories: list[dict]) -> str:
        from services import memory
        return memory.render_memory_recall(memories)

    async def store_memory(
        self,
        *,
        session: Any,
        repo_id: int,
        content: str,
        memory_type: str = "experience",
        task_id: int | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        from services import memory
        await memory.store_memory(
            session,
            repo_id=repo_id,
            content=content,
            memory_type=memory_type,
            task_id=task_id,
            metadata=metadata,
        )

    async def corpus_index(self, **kwargs: Any) -> None:
        pass

    async def archive_stale_memories(self, **kwargs: Any) -> int:
        return 0

    async def predict_outcome(self, **kwargs: Any) -> dict | None:
        return None

    async def store_decision(self, **kwargs: Any) -> None:
        pass

    async def search_memory_iterative(self, **kwargs: Any) -> list[dict]:
        return []

    # ── Plan gate ───────────────────────────────────────────────────
    async def run_plan_gate(self, **kwargs: Any) -> str:
        """Returns empty string = no approved plan (skip the gate)."""
        return ""

    async def get_preflight_allowlist(self, **kwargs: Any) -> list[str] | None:
        """Returns None = no allow-list enforcement."""
        return None

    # ── PR preflight ────────────────────────────────────────────────
    async def run_preflight(
        self,
        *,
        worktree_path: str,
        base_branch: str,
        allowlist: Any = None,
        skip_author_check: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Deterministic pre-push checks: secrets, forbidden files, size, author."""
        from services import pr_preflight
        return await pr_preflight.run_preflight(
            worktree_path=worktree_path,
            base_branch=base_branch,
            allowlist=allowlist,
            skip_author_check=skip_author_check,
        )

    def summarise_preflight(self, result: Any) -> dict:
        if isinstance(result, _SyntheticPreflightResult):
            return {"ok": True, "files_changed": 0, "violations_by_kind": {}, "violations": [], "stats": {}}
        from services import pr_preflight
        return pr_preflight.summarise(result)

    # ── Patch export ────────────────────────────────────────────────
    async def generate_patches(self, **kwargs: Any) -> None:
        pass

    # ── Budget circuit breaker ──────────────────────────────────────
    def check_budget(
        self,
        *,
        cum_cost: Decimal,
        cum_wall_ms: int,
        max_cost_usd: Decimal | None,
        max_wall_seconds: int | None,
        claude_mode: str,
        **kwargs: Any,
    ) -> tuple[str, str]:
        """Return ("ok"|"warn"|"exceeded", reason) for the task's budget caps.

        Warns at 80% of either cap and trips at 100%. Cost caps are skipped
        in subscription ("max") mode where per-call cost is not billed.
        """
        if max_cost_usd is not None and claude_mode != "max":
            if cum_cost >= max_cost_usd:
                return "exceeded", f"cost ${cum_cost} exceeded budget ${max_cost_usd}"
            if cum_cost >= max_cost_usd * Decimal(str(_BUDGET_WARN_THRESHOLD)):
                return "warn", f"cost ${cum_cost} at {_BUDGET_WARN_THRESHOLD:.0%} of ${max_cost_usd}"

        if max_wall_seconds is not None:
            cum_wall_s = cum_wall_ms / 1000
            if cum_wall_s >= max_wall_seconds:
                return "exceeded", f"wall-clock {cum_wall_s:.0f}s exceeded budget {max_wall_seconds}s"
            if cum_wall_s >= max_wall_seconds * _BUDGET_WARN_THRESHOLD:
                return "warn", f"wall-clock {cum_wall_s:.0f}s at {_BUDGET_WARN_THRESHOLD:.0%} of {max_wall_seconds}s"

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
