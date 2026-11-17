"""A2A push-notification NotifyBackend.

Delivers task lifecycle events to the webhook URLs external A2A peers
registered via ``tasks/pushNotificationConfig/create``. This is the
"agent is not sitting on an open connection" half of A2A observability —
the other half is the SSE stream in ``apps/web/src/lib/a2a/stream.ts``.

Unlike Telegram/Discord this backend has no single global destination: it
resolves ``a2a_push_configs`` rows for the specific task each event names,
so with no registered peers it is silently inert — ``a2a_push_configs`` is
only populated by the A2A gateway (``apps/web/src/app/api/a2a/route.ts``).

Everything is best-effort: a peer whose endpoint is down or slow is logged
and skipped, never retried into the task's critical path.

SSRF: the gateway validates the URL when it is registered
(``lib/a2a/push.ts``), but a hostname can re-resolve to a private address
later (DNS rebinding) — and the worker itself listens unauthenticated on
127.0.0.1:8000. So every delivery re-resolves the host, rejects it if ANY
address is non-public (same blocklist as ``push.ts``), and connects to the
validated IP directly: the URL host is replaced by the pinned IP, the
original ``Host`` header is kept, and for https the original hostname is sent
as SNI and verified against the certificate (httpcore ``sni_hostname``).
Redirects are never followed and proxy env vars are ignored (a proxy would
re-resolve the name itself, defeating the pin).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text

from models.base import async_session

from .base import NotifyBackend

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# DevServer task → A2A TaskState.
#
# KEEP IN SYNC with ``toA2AState`` / ``isAwaitingPlanDecision`` in
# apps/web/src/lib/a2a/mapping.ts, which is the authority — ``tasks/get`` and a
# push notification must report the same state for the same task row.
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


def _is_awaiting_plan_decision(row: dict[str, Any]) -> bool:
    """Mirror of ``isAwaitingPlanDecision`` (mapping.ts)."""
    return bool(
        row.get("mode") == "interactive"
        and row.get("has_plan")
        and not row.get("plan_approved_at")
        and not row.get("plan_rejected_at")
    )


def _a2a_state(row: dict[str, Any]) -> str:
    """Mirror of ``toA2AState`` (mapping.ts)."""
    # The reality gate refused the work outright.
    if row.get("abstain_reason"):
        return "rejected"
    status = row.get("status")
    if status in ("running", "verifying"):
        return "input-required" if _is_awaiting_plan_decision(row) else "working"
    return _STATE_MAP.get(str(status), "unknown")


# Non-public destinations. KEEP IN SYNC with BLOCKED_IPV4_SUBNETS /
# BLOCKED_IPV6_SUBNETS in apps/web/src/lib/a2a/push.ts.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)


def is_blocked_address(ip: str) -> bool:
    """True when ``ip`` is not a public unicast address (or not an IP at all)."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0].strip("[]"))
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr.version == net.version and addr in net for net in _BLOCKED_NETWORKS)


class UnsafeDestination(Exception):
    """The push URL is not (or no longer) a public http(s) endpoint."""


async def resolve_safe_destination(url: str) -> tuple[httpx.URL, str]:
    """Validate ``url`` at send time and return ``(pinned_url, hostname)``.

    Resolves the host now, rejects it if ANY resolved address is non-public,
    and returns the URL rewritten to the first validated IP so the connection
    cannot be steered elsewhere by a second DNS answer.
    """
    try:
        parsed = httpx.URL(url)
    except Exception as exc:
        raise UnsafeDestination("invalid url") from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise UnsafeDestination("url must be http(s) with a host")
    if parsed.userinfo:
        raise UnsafeDestination("url must not carry credentials")

    host = parsed.host
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise UnsafeDestination("host does not resolve") from exc
        addresses = [str(info[4][0]) for info in infos]
    if not addresses:
        raise UnsafeDestination("host does not resolve")
    if any(is_blocked_address(a) for a in addresses):
        raise UnsafeDestination("host resolves to a non-public address")
    return parsed.copy_with(host=addresses[0].split("%", 1)[0]), host


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
                    (
                        await db.execute(
                            text(
                                """
                            SELECT c.url, c.token, c.auth_scheme, c.auth_secret,
                                   t.id AS task_id, t.repo_id, t.status,
                                   t.mode, t.abstain_reason,
                                   t.plan_approved_at, t.plan_rejected_at,
                                   EXISTS (SELECT 1 FROM task_runs tr
                                            WHERE tr.task_id = t.id
                                              AND tr.plan_json IS NOT NULL)
                                       AS has_plan
                              FROM a2a_push_configs c
                              JOIN tasks t ON t.id = c.task_id
                             WHERE t.task_key = :key
                            """
                            ),
                            {"key": task_key},
                        )
                    )
                    .mappings()
                    .all()
                )
            return [dict(r) for r in rows]
        except Exception:
            # A missing table (very old database) or a transient DB error must
            # never break the notification fan-out for other channels.
            logger.debug("a2a push config lookup failed for %s", task_key, exc_info=True)
            return []

    async def _deliver(
        self, task_key: str, *, final: bool, extra: dict[str, Any] | None = None
    ) -> bool:
        configs = await self._configs_for(task_key)
        if not configs:
            return False

        ok_any = False
        for cfg in configs:
            state = _a2a_state(cfg)
            context_id = f"repo-{cfg['repo_id']}" if cfg["repo_id"] is not None else "no-repo"
            event: dict[str, Any] = {
                "kind": "status-update",
                "taskId": str(cfg["task_id"]),
                "contextId": context_id,
                "status": {
                    "state": state,
                    "timestamp": datetime.now(UTC).isoformat(),
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
                pinned, hostname = await resolve_safe_destination(str(cfg["url"]))
            except UnsafeDestination as exc:
                logger.warning("a2a push for %s skipped: %s", task_key, exc)
                continue
            # Keep the original authority for virtual hosting; the TCP
            # connection goes to the pinned IP.
            headers["Host"] = httpx.URL(str(cfg["url"])).netloc.decode("ascii")

            try:
                async with httpx.AsyncClient(
                    timeout=_TIMEOUT, follow_redirects=False, trust_env=False
                ) as client:
                    resp = await client.post(
                        pinned,
                        json={"jsonrpc": "2.0", "result": event},
                        headers=headers,
                        # TLS: SNI + certificate verification use the real
                        # hostname, not the IP we connect to.
                        extensions={"sni_hostname": hostname},
                    )
                if resp.status_code >= 300:
                    logger.warning(
                        "a2a push %s -> %s: %s",
                        task_key,
                        resp.status_code,
                        resp.text[:200],
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
