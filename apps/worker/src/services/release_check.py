"""Is a newer DevServer release out? — a cached, silent GitHub Releases check.

Why it exists: operators running a self-hosted instance had no way to learn
that a new version shipped short of watching the repository. The dashboard
footer asks the worker (``GET /internal/update/status``); this module answers
by comparing the running version (repo-root ``version.json``) with the latest
GitHub release tag.

Invariants:
- Never raises and never blocks the caller for long: any failure (no
  network, rate limit, unexpected JSON, missing ``version.json``) yields
  status ``unknown`` and a debug log line — the footer then shows nothing.
- At most one network call per ``CACHE_TTL_SECONDS`` (6 h) per process for a
  successful lookup; a failed lookup is retried after
  ``FAILURE_TTL_SECONDS`` so a transient outage does not hide a release for
  six hours, yet an air-gapped host is not hammered either.
- The clock and HTTP client are injectable so the cache and the failure
  paths are testable without sleeping or touching the network.
- The comparison logic (``parse_version`` / ``compare_versions`` /
  ``build_status``) is pure.
- The request carries a generic User-Agent and no credentials or host
  details.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

LATEST_RELEASE_URL = "https://api.github.com/repos/sergiovision/DevServer/releases/latest"
USER_AGENT = "devserver-release-check"
REQUEST_TIMEOUT_SECONDS = 10.0
CACHE_TTL_SECONDS = 6 * 60 * 60
FAILURE_TTL_SECONDS = 30 * 60

UpdateStatusName = Literal["up_to_date", "update_available", "unknown", "disabled"]
Version = tuple[int, int, int]

# `v1.0.21`, `1.0.21`, and the web's build string `1.0.20.dev` / `1.0.21-rc1`:
# the first three numeric components are the version; anything after is a
# label that does not take part in the comparison.
_VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[.+-][0-9A-Za-z.+-]*)?$")


@dataclass(frozen=True)
class ReleaseStatus:
    status: UpdateStatusName
    current: str | None
    latest: str | None
    release_url: str | None
    checked_at: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class LatestRelease:
    """What GitHub said, or ``tag=None`` when the lookup failed."""

    tag: str | None
    url: str | None
    checked_at: str


@dataclass(frozen=True)
class _CacheEntry:
    release: LatestRelease
    expires_at: float


_cache: _CacheEntry | None = None


# ─── Pure comparison ─────────────────────────────────────────────────────────


def parse_version(raw: str | None) -> Version | None:
    """``"v1.0.21"`` → ``(1, 0, 21)``; None for anything that is not a version."""
    if not raw:
        return None
    match = _VERSION_RE.match(raw.strip())
    if not match:
        return None
    major, minor, build = (int(part) for part in match.groups())
    return (major, minor, build)


def compare_versions(left: Version, right: Version) -> int:
    """-1 / 0 / 1 like a classic ``cmp``."""
    return (left > right) - (left < right)


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def build_status(
    current: str | None,
    latest_tag: str | None,
    release_url: str | None = None,
    checked_at: str | None = None,
) -> ReleaseStatus:
    """Decide the footer status from the running and the latest version."""
    current_version = parse_version(current)
    latest_version = parse_version(latest_tag)
    current_text = format_version(current_version) if current_version else current
    if current_version is None or latest_version is None:
        return ReleaseStatus("unknown", current_text, None, None, checked_at)
    newer = compare_versions(latest_version, current_version) > 0
    return ReleaseStatus(
        "update_available" if newer else "up_to_date",
        current_text,
        format_version(latest_version),
        release_url,
        checked_at,
    )


def disabled_status(current: str | None) -> ReleaseStatus:
    current_version = parse_version(current)
    return ReleaseStatus(
        "disabled", format_version(current_version) if current_version else None, None, None, None
    )


# ─── Running version ─────────────────────────────────────────────────────────


def _version_file_candidates() -> list[Path]:
    # Checkout: services/ → src/ → apps/worker/ → apps/ → repo root. The
    # parents walk also covers an image that copies version.json next to src/.
    here = Path(__file__).resolve()
    candidates = [parent / "version.json" for parent in here.parents]
    candidates += [Path("/app/version.json"), Path("/version.json")]
    return candidates


def read_current_version(candidates: list[Path] | None = None) -> str | None:
    """``major.minor.build`` from the first readable ``version.json``, else None."""
    for path in candidates if candidates is not None else _version_file_candidates():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return f"{int(data['major'])}.{int(data['minor'])}.{int(data['build'])}"
        except FileNotFoundError, NotADirectoryError, PermissionError:
            continue
        except Exception:
            logger.debug("release check: unreadable %s", path, exc_info=True)
            continue
    return None


# ─── Network + cache ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def fetch_latest_release(client: httpx.AsyncClient) -> LatestRelease:
    """One GitHub call. Never raises: failures return ``tag=None``."""
    checked_at = _now_iso()
    try:
        response = await client.get(
            LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            logger.debug("release check: GitHub answered %s", response.status_code)
            return LatestRelease(None, None, checked_at)
        body = response.json()
        tag = body.get("tag_name") if isinstance(body, dict) else None
        url = body.get("html_url") if isinstance(body, dict) else None
        if not isinstance(tag, str):
            return LatestRelease(None, None, checked_at)
        return LatestRelease(tag, url if isinstance(url, str) else None, checked_at)
    except Exception:
        logger.debug("release check failed", exc_info=True)
        return LatestRelease(None, None, checked_at)


async def get_latest_release(
    *,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LatestRelease:
    """The latest release, from the in-process cache when it is still fresh."""
    global _cache
    now = clock()
    if _cache is not None and now < _cache.expires_at:
        return _cache.release
    if client is not None:
        release = await fetch_latest_release(client)
    else:
        async with httpx.AsyncClient() as own_client:
            release = await fetch_latest_release(own_client)
    ttl = CACHE_TTL_SECONDS if release.tag is not None else FAILURE_TTL_SECONDS
    _cache = _CacheEntry(release, now + ttl)
    return release


async def get_update_status(
    *,
    enabled: bool = True,
    current: str | None = None,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReleaseStatus:
    """Compare the running version with the latest release.

    ``version.json`` wins; ``current`` is the fallback when no file is found —
    the web tier passes its build version because the worker image does not
    necessarily ship ``version.json``. ``enabled=False`` (the
    ``update_notify_enabled`` setting) answers ``disabled`` without any
    network call.
    """
    running = read_current_version() or current
    if not enabled:
        return disabled_status(running)
    if parse_version(running) is None:
        return build_status(running, None)
    release = await get_latest_release(client=client, clock=clock)
    return build_status(running, release.tag, release.url, release.checked_at)


def reset_cache() -> None:
    """Test hook: forget the cached release."""
    global _cache
    _cache = None
