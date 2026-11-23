"""``services.release_check``: version parsing, comparison, cache and failures."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from services import release_check
from services.release_check import (
    CACHE_TTL_SECONDS,
    build_status,
    compare_versions,
    get_update_status,
    parse_version,
    read_current_version,
)

RELEASE_BODY = {
    "tag_name": "v1.0.21",
    "html_url": "https://github.com/sergiovision/DevServer/releases/tag/v1.0.21",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def counting_client(
    handler_status: int = 200, body: object = RELEASE_BODY
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(handler_status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    release_check.reset_cache()
    # Pin the running version so tests do not depend on the checkout's file.
    monkeypatch.setattr(release_check, "read_current_version", lambda: "1.0.20")
    yield
    release_check.reset_cache()


# Feature: F-UPD-01
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v1.0.21", (1, 0, 21)),
        ("1.0.21", (1, 0, 21)),
        ("V2.10.3", (2, 10, 3)),
        ("1.0.20.dev", (1, 0, 20)),
        ("1.0.21-rc1", (1, 0, 21)),
        ("release-1", None),
        ("1.0", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_version_accepts_tags_with_and_without_v_prefix(
    raw: str | None, expected: tuple[int, int, int] | None
) -> None:
    assert parse_version(raw) == expected


# Feature: F-UPD-01
def test_compare_versions_is_numeric_not_lexical() -> None:
    assert compare_versions((1, 0, 10), (1, 0, 9)) == 1
    assert compare_versions((1, 0, 9), (1, 1, 0)) == -1
    assert compare_versions((1, 0, 20), (1, 0, 20)) == 0


# Feature: F-UPD-01
def test_build_status_reports_update_only_when_latest_is_newer() -> None:
    assert build_status("1.0.20", "v1.0.21").status == "update_available"
    assert build_status("1.0.21", "v1.0.21").status == "up_to_date"
    # A local build ahead of the latest release is not an "update".
    assert build_status("1.0.22", "v1.0.21").status == "up_to_date"


# Feature: F-UPD-01
def test_build_status_is_unknown_when_either_version_is_unparseable() -> None:
    assert build_status(None, "v1.0.21").status == "unknown"
    assert build_status("1.0.20", "nightly").status == "unknown"
    assert build_status("1.0.20", None).latest is None


# Feature: F-UPD-01
def test_read_current_version_reads_major_minor_build(tmp_path: Path) -> None:
    version_file = tmp_path / "version.json"
    version_file.write_text('{"major": 1, "minor": 0, "build": 20}', encoding="utf-8")
    # `read_current_version` here is the original function imported above; the
    # autouse fixture only replaces the module attribute that the service calls.
    assert read_current_version([tmp_path / "missing.json", version_file]) == "1.0.20"
    assert read_current_version([tmp_path / "missing.json"]) is None


# Feature: F-UPD-01
async def test_update_available_carries_latest_and_release_url() -> None:
    client, _ = counting_client()
    status = await get_update_status(client=client, clock=FakeClock())
    assert status.to_dict() == {
        "status": "update_available",
        "current": "1.0.20",
        "latest": "1.0.21",
        "release_url": RELEASE_BODY["html_url"],
        "checked_at": status.checked_at,
    }
    assert status.checked_at is not None


# Feature: F-UPD-03
async def test_network_failure_yields_unknown_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    status = await get_update_status(client=client, clock=FakeClock())
    assert status.status == "unknown"
    assert status.latest is None


# Feature: F-UPD-03
@pytest.mark.parametrize(
    ("http_status", "body"), [(403, {"message": "rate limited"}), (200, ["not", "an", "object"])]
)
async def test_bad_github_answer_yields_unknown(http_status: int, body: object) -> None:
    client, _ = counting_client(http_status, body)
    status = await get_update_status(client=client, clock=FakeClock())
    assert status.status == "unknown"


# Feature: F-UPD-04
async def test_cache_serves_six_hours_then_refetches() -> None:
    client, requests = counting_client()
    clock = FakeClock()
    await get_update_status(client=client, clock=clock)
    clock.now += CACHE_TTL_SECONDS - 1
    await get_update_status(client=client, clock=clock)
    assert len(requests) == 1
    clock.now += 2
    await get_update_status(client=client, clock=clock)
    assert len(requests) == 2


# Feature: F-UPD-04
async def test_failed_lookup_is_retried_sooner_than_six_hours() -> None:
    client, requests = counting_client(500, {})
    clock = FakeClock()
    await get_update_status(client=client, clock=clock)
    clock.now += release_check.FAILURE_TTL_SECONDS + 1
    await get_update_status(client=client, clock=clock)
    assert len(requests) == 2


# Feature: F-UPD-04
async def test_request_uses_generic_user_agent() -> None:
    client, requests = counting_client()
    await get_update_status(client=client, clock=FakeClock())
    assert requests[0].headers["User-Agent"] == release_check.USER_AGENT


# Feature: F-UPD-05
async def test_disabled_setting_skips_the_network_call() -> None:
    client, requests = counting_client()
    status = await get_update_status(enabled=False, client=client, clock=FakeClock())
    assert status.status == "disabled"
    assert status.current == "1.0.20"
    assert requests == []
