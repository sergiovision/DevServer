"""Remote (HTTP) mode for the ``DevServer`` MCP server.

By default this server runs over **stdio**, next to a coding CLI, on the
operator's own machine — no authentication, because the transport itself is the
trust boundary (a local pipe to a process you started).

Remote mode swaps that for Streamable HTTP so an off-box agent can connect. The
transport is then no longer a trust boundary, so two things become mandatory:

1. **Authentication** — every request must carry a DevServer A2A peer token,
   verified against ``a2a_peers`` through the Next.js introspection endpoint.
   The same credential the A2A gateway issues; there is no second credential
   system to manage or revoke.
2. **Per-request scoping** — a peer may only touch repos in its
   ``allowed_repo_ids``.

Point (2) is why remote mode cannot simply reuse the stdio code paths: the
stdio server resolves the repo once from the console's cwd and caches it in a
module global. In a multi-tenant HTTP process that cache would serve one peer's
repository memory to another. Remote mode therefore resolves the repo from the
*calling peer's* scope on every request and never caches across requests.

DEPLOYMENT: expose **this** server, never the worker. The worker has no
authentication on any route and serves ``/internal/env`` (cleartext secrets)
from the same namespace as the memory lanes this server proxies. Remote mode
works precisely because it is a scope-enforcing front door in front of a
service that has no front door of its own.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token

# ── Config ───────────────────────────────────────────────────────────────────

TRANSPORT = os.environ.get("DEVSERVER_MCP_TRANSPORT", "stdio").strip().lower()
HOST = os.environ.get("DEVSERVER_MCP_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("DEVSERVER_MCP_PORT", "8900"))
# The Next.js tier — owner of a2a_peers and of the governed task gateway.
WEB_URL = os.environ.get("DEVSERVER_WEB_URL", "http://localhost:3200").rstrip("/")

#: Introspection results are cached briefly so a chatty client does not issue
#: one HTTP round-trip per tool call. Short enough that revoking a peer takes
#: effect promptly — revocation latency is bounded by this value.
_CACHE_TTL_SECONDS = 30


def is_remote() -> bool:
    """True when this process is serving remote peers over HTTP."""
    return TRANSPORT in ("http", "streamable-http", "sse")


# ── Peer scope ───────────────────────────────────────────────────────────────


@dataclass
class PeerScope:
    peer_id: int
    slug: str
    name: str
    allowed_repo_ids: list[int] = field(default_factory=list)
    allowed_task_types: list[str] = field(default_factory=list)

    def may_use_repo(self, repo_id: int) -> bool:
        return repo_id in self.allowed_repo_ids

    def to_claims(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "slug": self.slug,
            "name": self.name,
            "allowed_repo_ids": self.allowed_repo_ids,
            "allowed_task_types": self.allowed_task_types,
        }

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> PeerScope:
        return cls(
            peer_id=int(claims.get("peer_id", 0)),
            slug=str(claims.get("slug", "")),
            name=str(claims.get("name", "")),
            allowed_repo_ids=list(claims.get("allowed_repo_ids") or []),
            allowed_task_types=list(claims.get("allowed_task_types") or []),
        )


class ScopeError(Exception):
    """A peer asked for something outside its grant. Message is shown verbatim."""


def current_peer() -> PeerScope | None:
    """Scope of the peer behind the in-flight request, or None in stdio mode."""
    if not is_remote():
        return None
    token = get_access_token()
    if token is None or not token.claims:
        raise ScopeError("not authenticated")
    return PeerScope.from_claims(token.claims)


def resolve_repo(explicit: int | None, peer: PeerScope) -> int:
    """Pick the repo for a remote request and authorize it.

    An explicit ``repo_id`` wins. Otherwise a peer scoped to exactly one repo
    gets that one — the common single-project credential — and a peer with
    several must say which, because guessing would silently read the wrong
    repository's memory.
    """
    if explicit is not None:
        if not peer.may_use_repo(explicit):
            raise ScopeError(
                f"not authorized for repo_id {explicit}; "
                f"this credential covers {peer.allowed_repo_ids or 'no repos'}"
            )
        return explicit
    if len(peer.allowed_repo_ids) == 1:
        return peer.allowed_repo_ids[0]
    if not peer.allowed_repo_ids:
        raise ScopeError("this credential is not scoped to any repository")
    raise ScopeError(
        "repo_id is required — this credential covers multiple repos: "
        f"{peer.allowed_repo_ids}"
    )


# ── Token verification ───────────────────────────────────────────────────────


class DevServerTokenVerifier(TokenVerifier):
    """Verifies a bearer token against ``a2a_peers`` via the Next.js tier.

    Kept deliberately thin: this server holds no database handle and no copy of
    the peer table. ``lib/a2a/auth.ts`` remains the one implementation of what a
    valid peer is, so revoking a credential in the dashboard revokes it here too
    (within ``_CACHE_TTL_SECONDS``).
    """

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, tuple[float, PeerScope]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None

        now = time.monotonic()
        cached = self._cache.get(token)
        if cached and cached[0] > now:
            return self._access_token(token, cached[1])

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                resp = await client.get(
                    f"{WEB_URL}/api/a2a/introspect",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            # Fail closed. An unreachable introspection endpoint must not become
            # an authentication bypass.
            return None

        if resp.status_code != 200:
            # Negative results are not cached: a peer re-enabled in the
            # dashboard should work on its next call, not 30s later.
            self._cache.pop(token, None)
            return None

        try:
            data = resp.json()
        except ValueError:
            return None
        if not data.get("active"):
            return None

        scope = PeerScope(
            peer_id=int(data.get("peer_id", 0)),
            slug=str(data.get("slug", "")),
            name=str(data.get("name", "")),
            allowed_repo_ids=list(data.get("allowed_repo_ids") or []),
            allowed_task_types=list(data.get("allowed_task_types") or []),
        )
        self._cache[token] = (now + _CACHE_TTL_SECONDS, scope)
        return self._access_token(token, scope)

    @staticmethod
    def _access_token(token: str, scope: PeerScope) -> AccessToken:
        return AccessToken(
            token=token,
            client_id=scope.slug or "a2a-peer",
            scopes=[],
            claims=scope.to_claims(),
        )


# ── Governed task gateway ────────────────────────────────────────────────────


async def a2a_rpc(token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    """Call the A2A JSON-RPC gateway as the calling peer.

    Remote task tools route through here rather than hitting the worker's
    ``/internal/tasks/*`` directly, so a task created over MCP passes the exact
    same governance a task created over A2A does: repo + task-type allow-list,
    forced interactive mode and patch-only git flow, spend ceilings, and the
    daily quota. Bypassing it would make MCP a hole in the policy the operator
    configured on the credential.
    """
    payload = {"jsonrpc": "2.0", "id": "mcp", "method": method, "params": params or {}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
        resp = await client.post(
            f"{WEB_URL}/api/a2a",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code == 401:
        raise ScopeError("peer token rejected by the A2A gateway")
    if resp.status_code == 403:
        raise ScopeError("peer is disabled")
    try:
        data = resp.json()
    except ValueError:
        raise ScopeError(
            f"A2A gateway returned a non-JSON response ({resp.status_code})"
        ) from None
    if "error" in data:
        err = data["error"] or {}
        raise ScopeError(str(err.get("message") or "A2A gateway error"))
    return data.get("result")


def current_token() -> str:
    """Raw bearer token of the in-flight request (to re-present to the gateway)."""
    token = get_access_token()
    if token is None:
        raise ScopeError("not authenticated")
    return token.token
