"""A2A push backend: delivery-time SSRF checks and state mapping parity."""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from services.notify import a2a_backend
from services.notify.a2a_backend import (
    UnsafeDestination,
    _a2a_state,
    is_blocked_address,
    resolve_safe_destination,
)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "198.18.0.1",
        "192.0.0.8",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "ff02::1",
        "not-an-ip",
    ],
)
def test_blocked_addresses(ip: str) -> None:
    assert is_blocked_address(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip: str) -> None:
    assert not is_blocked_address(ip)


def _fake_getaddrinfo(answers: list[str]):
    async def fake(host: str, port: int, **_: Any):
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in answers
        ]

    return fake


async def test_rebinding_to_loopback_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34", "127.0.0.1"]))
    with pytest.raises(UnsafeDestination):
        await resolve_safe_destination("http://rebind.example.com/hook")


async def test_public_host_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
    pinned, host = await resolve_safe_destination("https://peer.example.com:8443/cb?x=1")
    assert host == "peer.example.com"
    assert pinned == httpx.URL("https://93.184.216.34:8443/cb?x=1")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/internal/env",
        "http://[::1]:8000/",
        "ftp://example.com/",
        "http://user:pw@8.8.8.8/",
    ],
)
async def test_literal_and_scheme_refusals(url: str) -> None:
    with pytest.raises(UnsafeDestination):
        await resolve_safe_destination(url)


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "running",
        "mode": "autonomous",
        "abstain_reason": None,
        "plan_approved_at": None,
        "plan_rejected_at": None,
        "has_plan": False,
    }
    base.update(kw)
    return base


def test_state_mapping_matches_web_mapping() -> None:
    assert _a2a_state(_row()) == "working"
    assert _a2a_state(_row(status="test")) == "completed"
    assert _a2a_state(_row(status="blocked")) == "input-required"
    assert _a2a_state(_row(status="cancelled")) == "canceled"
    assert _a2a_state(_row(status="weird")) == "unknown"
    # abstain wins over status
    assert _a2a_state(_row(status="blocked", abstain_reason="low score")) == "rejected"
    # undecided plan on an interactive task
    assert _a2a_state(_row(mode="interactive", has_plan=True)) == "input-required"
    assert _a2a_state(_row(mode="interactive", has_plan=False)) == "working"
    assert (
        _a2a_state(_row(mode="interactive", has_plan=True, plan_approved_at="2026-01-01"))
        == "working"
    )


def test_module_exports_blocklist() -> None:
    # Guard against the list silently shrinking.
    assert len(a2a_backend._BLOCKED_NETWORKS) >= 28
