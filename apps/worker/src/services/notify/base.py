"""NotifyBackend — abstract channel adapter.

A NotifyBackend receives strongly-typed events (task started, done,
failed, budget alerts, vendor failover, preflight block) and translates
them into whatever the destination channel speaks (Telegram Markdown,
Slack Blocks, Discord Embeds, Webhook POST, SMTP, PagerDuty, …).

Everything is best-effort. No method ever raises — a failed send logs
and returns False, the task never aborts because a notification
channel was down.

Default implementations are provided for every event so a new backend
only has to override ``send_text`` to get plain-text fallbacks for
free. Override individual event methods when you want richer formatting.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class NotifyBackend(ABC):
    """Abstract channel adapter. Subclass + implement ``send_text``.

    Event methods have sensible plain-text defaults that call ``send_text``.
    Override an event method to use the channel's native rich formatting
    (Telegram Markdown, Slack Blocks, Discord Embeds, ...).
    """

    #: machine-readable channel id used in logs (e.g. "telegram", "slack").
    channel: str = "base"

    def is_configured(self) -> bool:
        """Return True if this backend has the env vars it needs.

        The dispatcher skips un-configured backends silently.
        """
        return False

    # ── Required primitive ────────────────────────────────────────────

    @abstractmethod
    async def send_text(self, message: str) -> bool:
        """Send a plain-text message. Return True on success, False on failure."""
        raise NotImplementedError

    # ── Event methods (default: render as plain text) ─────────────────

    async def send_task_start(
        self,
        *,
        task_key: str,
        title: str,
        repo_name: str,
        mode: str,
        vendor: str,
        model: str,
    ) -> bool:
        return await self.send_text(
            f"[start] {task_key}: {title}\n"
            f"repo={repo_name} agent={vendor}/{model} mode={mode}"
        )

    async def send_task_success(
        self,
        *,
        task_key: str,
        git_flow: str,
        pr_url: str | None,
        attempts: int,
        turns: int,
        cost: Decimal,
        duration_ms: int,
        repo_name: str,
    ) -> bool:
        duration_min = (duration_ms or 0) / 1000 / 60
        pr_line = f"\nPR: {pr_url}" if pr_url else ""
        return await self.send_text(
            f"[OK] {task_key} done ({git_flow}){pr_line}\n"
            f"repo={repo_name} attempts={attempts} turns={turns} "
            f"cost=${cost:.4f} duration={duration_min:.1f}m"
        )

    async def send_task_failed(
        self,
        *,
        task_key: str,
        repo_name: str,
        error_context: str,
        attempts: int,
        cost: Decimal,
    ) -> bool:
        excerpt = (error_context or "").strip()[:400]
        return await self.send_text(
            f"[FAIL] {task_key}\n"
            f"repo={repo_name} attempts={attempts} cost=${cost:.4f}\n\n"
            f"{excerpt}"
        )

    async def send_vendor_failover(
        self,
        *,
        task_key: str,
        repo_name: str,
        from_vendor: str,
        from_model: str,
        to_vendor: str,
        to_model: str,
    ) -> bool:
        return await self.send_text(
            f"[failover] {task_key}\n"
            f"repo={repo_name}\n"
            f"{from_vendor}/{from_model} -> {to_vendor}/{to_model}"
        )

    async def send_budget_warning(
        self,
        *,
        task_key: str,
        repo_name: str,
        reason: str,
        cum_cost: Decimal,
        cum_wall_ms: int,
        max_cost: Decimal | None,
        max_wall: int | None,
    ) -> bool:
        wall_s = (cum_wall_ms or 0) / 1000
        return await self.send_text(
            f"[budget-warn] {task_key}\n"
            f"repo={repo_name} cost=${cum_cost:.4f} wall={wall_s:.0f}s\n"
            f"{reason}"
        )

    async def send_budget_exceeded(
        self,
        *,
        task_key: str,
        repo_name: str,
        reason: str,
        cum_cost: Decimal,
        cum_wall_ms: int,
    ) -> bool:
        wall_s = (cum_wall_ms or 0) / 1000
        return await self.send_text(
            f"[budget-exceeded] {task_key} BLOCKED\n"
            f"repo={repo_name} cost=${cum_cost:.4f} wall={wall_s:.0f}s\n"
            f"{reason}"
        )

    async def send_preflight_blocked(
        self,
        *,
        task_key: str,
        violations: list[dict],
    ) -> bool:
        lines = [f"[preflight-block] {task_key}"]
        for v in violations[:5]:
            lines.append(
                f"  - {v.get('kind', 'unknown')}: "
                f"{(v.get('detail') or '')[:150]} "
                f"({v.get('severity', 'hard')})"
            )
        if len(violations) > 5:
            lines.append(f"  ...+{len(violations) - 5} more")
        return await self.send_text("\n".join(lines))

    async def send_operator_message(
        self,
        *,
        from_task_key: str,
        subject: str | None,
        body: str,
        kind: str,
    ) -> bool:
        """An agent messaged ``operator`` — surface it to every channel."""
        subj = f" — {subject}" if subject else ""
        excerpt = body.strip()
        if len(excerpt) > 400:
            excerpt = excerpt[:400] + "…"
        return await self.send_text(
            f"[msg from {from_task_key}] ({kind}){subj}\n\n{excerpt}"
        )

    # ── Safe wrapper helpers ──────────────────────────────────────────

    async def safe(self, coro: Any) -> bool:
        """Run a backend send with a try/except wrapper so a failure
        never aborts the caller. Internal use only."""
        try:
            return bool(await coro)
        except Exception:
            logger.exception("notify backend %s send failed", self.channel)
            return False
