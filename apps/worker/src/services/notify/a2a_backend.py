"""A2A push-notification NotifyBackend.

Delivers task lifecycle events to the webhook URLs external A2A peers
registered via ``tasks/pushNotificationConfig/create``. This is the
"agent is not sitting on an open connection" half of A2A observability —
the other half is the SSE stream in ``apps/web/src/lib/a2a/stream.ts``.

Unlike Telegram/Discord this backend has no single global destination: it
resolves ``a2a_push_configs`` rows for the specific task each event names,
so with no registered peers it is silently inert. That makes it safe to
leave in the free edition — ``a2a_push_configs`` is always present (like
``agent_memory``), just never populated when the Pro gateway is stripped.

Everything is best-effort: a peer whose endpoint is down or slow is logged
and skipped, never retried into the task's critical path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text

from models.base import async_session

from .base import NotifyBackend

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# DevServer status → A2A TaskState.
#
# KEEP IN SYNC with ``toA2AState`` in apps/web/src/lib/a2a/mapping.ts, which is
# the authority (it also handles the abstain/plan-gate cases that need columns
# beyond `status`). Only the plain status mapping is needed here, because a
# push notification always fires on a concrete lifecycle event.
#
# Note `test` — the worker writes it, not `done`, on success.
_STATE_MAP = {
    "pending": "submitted",
    "queued": "submitted",
    "running": "working",
    "verifying": "working",
    "test": "completed",
    "done": "completed",
    "retired": "completed",
    "failed": "failed",
    "cancelled": "canceled",
    "blocked": "input-required",
}


class A2APushBackend(NotifyBackend):
    channel = "a2a"

    def is_configured(self) -> bool:
        # Always "configured": whether anything is delivered depends on
        # per-task rows, resolved at send time. The cost of being wrong is one
        # indexed lookup that returns zero rows.
        return True

    async def send_text(self, message: str) -> bool:
        # A2A notifications are per-task; a channel-wide broadcast has no
        # meaningful destination here. Events without a task_key are dropped.
        return False

    # ── delivery ──────────────────────────────────────────────────────

    async def _configs_for(self, task_key: str) -> list[dict[str, Any]]:
        """Resolve push configs registered against a task key."""
        try:
            async with async_session() as db:
                rows = (
                    await db.execute(
                        text(
                            """
                            SELECT c.url, c.token, c.auth_scheme, c.auth_secret,
                                   t.id AS task_id, t.repo_id, t.status
                              FROM a2a_push_configs c
                              JOIN tasks t ON t.id = c.task_id
                             WHERE t.task_key = :key
                            """
                        ),
                        {"key": task_key},
                    )
                ).mappings().all()
            return [dict(r) for r in rows]
        except Exception:
            # A missing table (very old database) or a transient DB error must
            # never break the notification fan-out for other channels.
            logger.debug("a2a push config lookup failed for %s", task_key, exc_info=True)
            return []

    async def _deliver(self, task_key: str, *, final: bool, extra: dict[str, Any] | None = None) -> bool:
        configs = await self._configs_for(task_key)
        if not configs:
            return False

        ok_any = False
        for cfg in configs:
            state = _STATE_MAP.get(cfg["status"], "unknown")
            context_id = f"repo-{cfg['repo_id']}" if cfg["repo_id"] is not None else "no-repo"
            event: dict[str, Any] = {
                "kind": "status-update",
                "taskId": str(cfg["task_id"]),
                "contextId": context_id,
                "status": {
                    "state": state,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "final": final,
            }
            if extra:
                event["metadata"] = extra

            headers = {"Content-Type": "application/json"}
            # The token is the peer's own opaque value, echoed back so it can
            # correlate the callback with the config it registered.
            if cfg.get("token"):
                headers["X-A2A-Notification-Token"] = str(cfg["token"])
            scheme = (cfg.get("auth_scheme") or "").lower()
            if scheme == "bearer" and cfg.get("auth_secret"):
                headers["Authorization"] = f"Bearer {cfg['auth_secret']}"
            elif scheme == "apikey" and cfg.get("auth_secret"):
                headers["X-API-Key"] = str(cfg["auth_secret"])

            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        str(cfg["url"]),
                        json={"jsonrpc": "2.0", "result": event},
                        headers=headers,
                    )
                if resp.status_code >= 300:
                    logger.warning(
                        "a2a push %s -> %s: %s",
                        task_key, resp.status_code, resp.text[:200],
                    )
                    continue
                ok_any = True
            except Exception:
                logger.warning("a2a push delivery failed for %s", task_key, exc_info=True)
        return ok_any

    # ── event overrides (only the ones a peer can act on) ─────────────

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
        return await self._deliver(
            task_key,
            final=True,
            extra={
                "prUrl": pr_url,
                "attempts": attempts,
                "turns": turns,
                "costUsd": float(cost or 0),
                "durationMs": duration_ms,
            },
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
        return await self._deliver(
            task_key,
            final=True,
            extra={"attempts": attempts, "costUsd": float(cost or 0)},
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
        # Budget exhaustion blocks the task — from the peer's side that is
        # `input-required`, not a terminal failure, so `final` stays False.
        return await self._deliver(
            task_key,
            final=False,
            extra={"reason": reason, "costUsd": float(cum_cost or 0)},
        )

    async def send_task_state_changed(self, *, task_key: str, final: bool) -> bool:
        # The lightweight runner's only lifecycle signal — without this, a peer
        # scoped to `research`/`skill` (the safe default for a new credential)
        # would register a push config and never be called back.
        return await self._deliver(task_key, final=final)

    async def send_preflight_blocked(
        self,
        *,
        task_key: str,
        violations: list[dict],
    ) -> bool:
        return await self._deliver(
            task_key,
            final=False,
            extra={
                "reason": "pr_preflight",
                # Kinds only — a violation detail can quote the secret that
                # triggered the scan, and this payload leaves the deployment.
                "violations": [v.get("kind", "unknown") for v in violations[:10]],
            },
        )
