"""Notification dispatcher — fans out every event to every configured backend.

Usage:

    from services.notify import notify

    await notify.task_success(
        task_key="ABC-1", git_flow="branch", pr_url="...",
        attempts=1, turns=12, cost=Decimal("0.45"),
        duration_ms=123_000, repo_name="mono",
    )

The dispatcher tries every configured backend in parallel and returns
when all of them finish (success or failure). A failing backend never
prevents another backend from firing, and no exception ever escapes.

Configuring a channel is zero-SQL — just set the relevant env vars
(TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL).
Unconfigured backends are skipped silently.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from .base import NotifyBackend
from .discord_backend import DiscordBackend
from .telegram_backend import TelegramBackend

logger = logging.getLogger(__name__)

__all__ = ["NotifyBackend", "Dispatcher", "notify"]


class Dispatcher:
    """Fans a typed event out to every configured backend.

    Construct once at module load; the list of backends is reread from
    settings each time so a restart picks up env changes.
    """

    def __init__(self) -> None:
        self._backends: list[NotifyBackend] = [
            TelegramBackend(),
            DiscordBackend(),
        ]

    def configured_channels(self) -> list[str]:
        return [b.channel for b in self._backends if b.is_configured()]

    async def _fanout(self, method: str, **kwargs: Any) -> dict[str, bool]:
        """Call ``method(**kwargs)`` on every configured backend in parallel."""
        configured = [b for b in self._backends if b.is_configured()]
        if not configured:
            return {}

        async def _run(backend: NotifyBackend) -> tuple[str, bool]:
            try:
                result = await getattr(backend, method)(**kwargs)
                return backend.channel, bool(result)
            except Exception:
                logger.exception(
                    "notify backend=%s method=%s raised", backend.channel, method,
                )
                return backend.channel, False

        pairs = await asyncio.gather(*[_run(b) for b in configured])
        return dict(pairs)

    # ── Typed event methods ─────────────────────────────────────────────

    async def text(self, message: str) -> dict[str, bool]:
        """Plain-text fan-out. For anything without a dedicated event method."""
        return await self._fanout("send_text", message=message)

    async def task_start(
        self,
        *,
        task_key: str,
        title: str,
        repo_name: str,
        mode: str,
        vendor: str,
        model: str,
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_task_start",
            task_key=task_key, title=title, repo_name=repo_name,
            mode=mode, vendor=vendor, model=model,
        )

    async def task_success(
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
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_task_success",
            task_key=task_key, git_flow=git_flow, pr_url=pr_url,
            attempts=attempts, turns=turns, cost=cost,
            duration_ms=duration_ms, repo_name=repo_name,
        )

    async def task_failed(
        self,
        *,
        task_key: str,
        repo_name: str,
        error_context: str,
        attempts: int,
        cost: Decimal,
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_task_failed",
            task_key=task_key, repo_name=repo_name,
            error_context=error_context, attempts=attempts, cost=cost,
        )

    async def vendor_failover(
        self,
        *,
        task_key: str,
        repo_name: str,
        from_vendor: str,
        from_model: str,
        to_vendor: str,
        to_model: str,
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_vendor_failover",
            task_key=task_key, repo_name=repo_name,
            from_vendor=from_vendor, from_model=from_model,
            to_vendor=to_vendor, to_model=to_model,
        )

    async def budget_warning(
        self,
        *,
        task_key: str,
        repo_name: str,
        reason: str,
        cum_cost: Decimal,
        cum_wall_ms: int,
        max_cost: Decimal | None,
        max_wall: int | None,
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_budget_warning",
            task_key=task_key, repo_name=repo_name, reason=reason,
            cum_cost=cum_cost, cum_wall_ms=cum_wall_ms,
            max_cost=max_cost, max_wall=max_wall,
        )

    async def budget_exceeded(
        self,
        *,
        task_key: str,
        repo_name: str,
        reason: str,
        cum_cost: Decimal,
        cum_wall_ms: int,
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_budget_exceeded",
            task_key=task_key, repo_name=repo_name, reason=reason,
            cum_cost=cum_cost, cum_wall_ms=cum_wall_ms,
        )

    async def preflight_blocked(
        self,
        *,
        task_key: str,
        violations: list[dict],
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_preflight_blocked",
            task_key=task_key, violations=violations,
        )

    async def operator_message(
        self,
        *,
        from_task_key: str,
        subject: str | None,
        body: str,
        kind: str,
    ) -> dict[str, bool]:
        return await self._fanout(
            "send_operator_message",
            from_task_key=from_task_key, subject=subject,
            body=body, kind=kind,
        )


# Module-level singleton — import as ``from services.notify import notify``.
notify = Dispatcher()
