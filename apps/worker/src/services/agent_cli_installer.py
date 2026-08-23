"""Lazy, on-demand installation of the vendor coding-agent CLIs.

WHY THIS EXISTS. The worker image used to bake all three vendor CLIs in:
``claude`` (316 MB), ``codex`` (259 MB) and ``agy`` (196 MB) — 771 MB, or 41% of
a 1.9 GB image, for four vendors of which a given deployment typically uses one.
They are now fetched the first time a task actually selects that vendor, into
:data:`settings.agent_cli_dir` (a persistent volume), and every later task finds
the binary already on ``PATH``.

The trade is deliberate and was chosen with eyes open: the worker image drops to
~1.03 GB, and in exchange the first task per vendor pays a one-off install
(~60–90 s) and needs egress to registry.npmjs.org / antigravity.google. An
air-gapped deployment sets ``AGENT_CLI_AUTO_INSTALL=false`` and pre-seeds the
volume by hand — this module is the only thing that installs a CLI, so the
Dockerfile has no bake-back path to keep in step with it.

KEYED ON THE BINARY, NOT THE VENDOR. ``anthropic`` and ``glm`` both run the
``claude`` binary (GLM is Claude Code pointed at Zhipu's endpoint), so an
install triggered by a GLM task also satisfies every Anthropic task. The lock
and the registry below are therefore keyed on the resolved binary name.

NEVER INSTALLS OVER AN OPERATOR'S CHOICE. If ``CLAUDE_BIN`` / ``CODEX_BIN`` /
``GEMINI_BIN`` names anything other than the stock binary — an absolute path,
a wrapper script, a renamed build — :func:`ensure_cli` refuses to install and
lets the existing "CLI not found" message stand. Managing that path is then the
operator's business, which is the whole point of the override.

This module is FREE-level (no ``pro`` dependency) and survives
``strip-pro.sh``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from config import settings

logger = logging.getLogger(__name__)

# fcntl is POSIX-only. The cross-process lock below degrades to the in-process
# asyncio lock on Windows, where the worker runs on the host anyway and two
# containers cannot be sharing an install volume.
try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


# ── Install specs ───────────────────────────────────────────────────────────
# One entry per *binary*, not per vendor. ``versions`` names the settings
# attribute holding the pin; an empty pin means "whatever the channel serves",
# which is the only thing the Antigravity release channel offers.


@dataclass(frozen=True)
class InstallSpec:
    """How to obtain one CLI binary."""

    #: The bare binary name, as it must appear on PATH afterwards.
    binary: str
    #: Human-readable name for logs and task events.
    label: str
    #: npm package (npm-installed CLIs only). Empty → custom installer.
    npm_package: str = ""
    #: ``config.settings`` attribute holding the pinned version. Empty pin
    #: value → install the channel's current release.
    version_setting: str = ""
    #: Rough download size, so the log line sets an honest expectation.
    approx_mb: int = 0


_SPECS: dict[str, InstallSpec] = {
    "claude": InstallSpec(
        binary="claude",
        label="Claude Code",
        npm_package="@anthropic-ai/claude-code",
        version_setting="claude_code_version",
        approx_mb=316,
    ),
    "codex": InstallSpec(
        binary="codex",
        label="Codex",
        npm_package="@openai/codex",
        version_setting="codex_version",
        approx_mb=259,
    ),
    "agy": InstallSpec(
        binary="agy",
        label="Antigravity",
        version_setting="agy_version",
        approx_mb=196,
    ),
}

# One lock per binary. Two concurrent tasks that both pick Anthropic must not
# both run `npm install -g` into the same prefix.
_locks: dict[str, asyncio.Lock] = {}
# Binaries whose install already failed this process. A missing CLI usually
# means no egress, and retrying a 90-second network timeout on every task in
# the queue turns one broken vendor into a stalled worker.
_failed: dict[str, str] = {}


def _lock_for(binary: str) -> asyncio.Lock:
    lock = _locks.get(binary)
    if lock is None:
        lock = asyncio.Lock()
        _locks[binary] = lock
    return lock


def _pinned_version(spec: InstallSpec) -> str:
    if not spec.version_setting:
        return ""
    return (getattr(settings, spec.version_setting, "") or "").strip()


def install_dir() -> str:
    """Root of the writable CLI install prefix (``/opt/agents`` in Docker)."""
    return (settings.agent_cli_dir or "").strip()


def _bin_dir() -> str:
    return os.path.join(install_dir(), "bin")


def is_supported(binary: str) -> bool:
    """True if this binary name is one we know how to fetch."""
    return binary in _SPECS


def is_enabled() -> bool:
    """True if lazy install is turned on and has somewhere to write."""
    return bool(settings.agent_cli_auto_install) and bool(install_dir())


# ── npm-installed CLIs ──────────────────────────────────────────────────────


async def _install_npm(spec: InstallSpec) -> None:
    """`npm install -g` into our own prefix rather than the system one.

    ``npm_config_prefix`` is what makes this land in the volume; the image's
    ``/usr/lib/node_modules`` is root-owned and the worker runs as uid 10001,
    so a plain global install would fail on permissions even if we wanted it.
    """
    version = _pinned_version(spec)
    package = f"{spec.npm_package}@{version}" if version else spec.npm_package

    prefix = install_dir()
    env = dict(os.environ)
    env["npm_config_prefix"] = prefix
    # Keep npm's cache inside the volume too. The default (~/.npm) lives in the
    # agent CLIs' own HOME, which is bind-mounted read-only in Max-subscription
    # deployments — npm then fails with EROFS before it downloads anything.
    env["npm_config_cache"] = os.path.join(prefix, ".npm-cache")
    env["npm_config_fund"] = "false"
    env["npm_config_audit"] = "false"
    env["npm_config_update_notifier"] = "false"

    proc = await asyncio.create_subprocess_exec(
        "npm", "install", "-g", package,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        tail = (out or b"").decode("utf-8", "replace").strip()[-1500:]
        raise RuntimeError(f"npm install {package} failed (exit {proc.returncode}): {tail}")

    # Drop the download cache. Measured: it leaves ~99 MB beside a 317 MB
    # install, and it is never read again — the next task finds the binary
    # already on PATH and never re-runs npm. The Dockerfile did the same for
    # the baked-in path; the volume deserves it more, because a volume is not
    # rebuilt. Best-effort: a stale cache is wasted disk, not a failed install.
    try:
        cleaner = await asyncio.create_subprocess_exec(
            "npm", "cache", "clean", "--force",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        await cleaner.communicate()
    except Exception as exc:  # noqa: BLE001
        logger.debug("npm cache clean after %s failed: %s", package, exc)


# ── Antigravity CLI ─────────────────────────────────────────────────────────
# `agy` is not on npm and its install.sh has no version flag, so we do exactly
# what the installer does — read the platform manifest, verify the published
# SHA-512, extract the binary. This mirrors the logic the Dockerfile used to
# carry; keep the two in step if the vendor changes the manifest shape.

_AGY_MANIFEST_BASE = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
)


def _agy_platform() -> str:
    """Manifest platform key for the running machine."""
    import platform

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"no Antigravity CLI build for architecture {machine!r}")

    system = platform.system().lower()
    if system not in ("linux", "darwin"):
        raise RuntimeError(f"no Antigravity CLI build for platform {system!r}")
    return f"{system}_{arch}"


async def _install_agy(spec: InstallSpec) -> None:
    import hashlib

    plat = _agy_platform()
    pinned = _pinned_version(spec)

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(f"{_AGY_MANIFEST_BASE}/manifests/{plat}.json")
        resp.raise_for_status()
        manifest = json.loads(resp.text)

        version = str(manifest.get("version", "")).strip()
        url = str(manifest.get("url", "")).strip()
        sha512 = str(manifest.get("sha512", "")).strip()
        if not url or not sha512:
            raise RuntimeError("could not parse the Antigravity release manifest")

        # AGY_VERSION is an assertion, not a pin — the channel serves one
        # release and offers no way to ask for an older one. Failing loudly is
        # closer to a pin than silently accepting a version nobody tested.
        if pinned and pinned != version:
            raise RuntimeError(
                f"AGY_VERSION={pinned} is pinned but the release channel serves "
                f"{version}. Test the new release, then update AGY_VERSION."
            )

        logger.info("Antigravity CLI release channel: %s", version)
        archive = await client.get(url)
        archive.raise_for_status()
        blob = archive.content

    digest = hashlib.sha512(blob).hexdigest()
    if digest != sha512.lower():
        raise RuntimeError(
            "Antigravity CLI checksum mismatch — refusing to install "
            f"(expected {sha512[:16]}…, got {digest[:16]}…)"
        )

    bin_dir = _bin_dir()
    os.makedirs(bin_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, "agy.tar.gz")
        with open(archive_path, "wb") as fh:
            fh.write(blob)
        with tarfile.open(archive_path, "r:gz") as tar:
            member = tar.getmember("antigravity")
            tar.extract(member, path=tmp)
        extracted = os.path.join(tmp, "antigravity")
        # Land it via a temp name + atomic rename so a concurrent reader never
        # sees a half-written binary on PATH.
        staged = os.path.join(bin_dir, ".agy.incoming")
        shutil.copyfile(extracted, staged)
        os.chmod(staged, 0o755)
        os.replace(staged, os.path.join(bin_dir, spec.binary))


# ── Public entry point ──────────────────────────────────────────────────────

_INSTALLERS: dict[str, Callable[[InstallSpec], Awaitable[None]]] = {
    "agy": _install_agy,
}


def _cross_process_lock(binary: str):
    """flock a per-binary lockfile so two worker containers serialise.

    Best-effort: returns ``None`` where fcntl is unavailable or the lock dir
    cannot be created. Losing the cross-process lock costs a duplicated
    download, not a corrupted install — npm and the agy path are both
    atomic at the point they publish the binary.
    """
    if fcntl is None:
        return None
    try:
        os.makedirs(install_dir(), exist_ok=True)
        handle = open(os.path.join(install_dir(), f".{binary}.lock"), "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    except OSError as exc:
        logger.debug("cross-process install lock unavailable for %s: %s", binary, exc)
        return None


async def ensure_cli(
    backend,
    *,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> bool:
    """Make ``backend``'s CLI runnable, installing it on first use.

    Returns True if the binary is available when this returns. False means the
    caller should fall back to its existing "CLI not found" path — the reason
    has already been logged and, where ``on_event`` was supplied, emitted.

    ``on_event(event_type, payload)`` is awaited for ``agent_cli_installing``,
    ``agent_cli_installed`` and ``agent_cli_install_failed`` so a task run can
    surface a 90-second install in the dashboard timeline instead of looking
    hung. Callers without a task context (the system LLM, the Ask Agent panel)
    pass nothing.
    """
    if backend.is_available():
        return True

    configured = backend.configured_bin
    spec = _SPECS.get(configured)

    # An operator-supplied path or a binary we have no recipe for: say so once
    # and let the caller's existing message explain the fix.
    if spec is None:
        if configured != backend.cli_bin:
            logger.warning(
                "%s CLI '%s' is a deployment override and is not on PATH — not "
                "auto-installing; fix %s or the PATH.",
                backend.label, configured, backend.bin_setting or "the CLI path",
            )
        return False

    if not is_enabled():
        logger.warning(
            "%s CLI '%s' is missing and auto-install is disabled "
            "(AGENT_CLI_AUTO_INSTALL / AGENT_CLI_DIR).",
            backend.label, configured,
        )
        return False

    if configured in _failed:
        logger.warning(
            "%s CLI install already failed this process, not retrying: %s",
            backend.label, _failed[configured],
        )
        return False

    async with _lock_for(configured):
        # Another coroutine may have installed it while we waited.
        if backend.is_available():
            return True
        if configured in _failed:
            return False

        version = _pinned_version(spec)
        logger.info(
            "%s CLI not present — installing %s%s (~%d MB) into %s",
            backend.label, spec.binary,
            f"@{version}" if version else " (channel current)",
            spec.approx_mb, install_dir(),
        )
        if on_event:
            await on_event("agent_cli_installing", {
                "vendor": backend.vendor,
                "binary": spec.binary,
                "version": version or "channel-current",
                "approx_mb": spec.approx_mb,
                "dir": install_dir(),
            })

        handle = await asyncio.to_thread(_cross_process_lock, configured)
        try:
            # A peer container may have finished while we blocked on flock.
            if backend.is_available():
                return True
            installer = _INSTALLERS.get(spec.binary)
            if installer is not None:
                await installer(spec)
            else:
                await _install_npm(spec)
        except Exception as exc:  # noqa: BLE001 — every failure degrades the same way
            reason = str(exc).strip() or exc.__class__.__name__
            _failed[configured] = reason
            logger.error("%s CLI install failed: %s", backend.label, reason)
            if on_event:
                await on_event("agent_cli_install_failed", {
                    "vendor": backend.vendor,
                    "binary": spec.binary,
                    "error": reason[:2000],
                })
            return False
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    # shutil.which caches nothing, but PATH must actually contain our bin dir.
    if not backend.is_available():
        reason = (
            f"{spec.binary} installed into {_bin_dir()} but is still not on the "
            f"worker's PATH — add that directory to PATH."
        )
        _failed[configured] = reason
        logger.error(reason)
        if on_event:
            await on_event("agent_cli_install_failed", {
                "vendor": backend.vendor, "binary": spec.binary, "error": reason,
            })
        return False

    logger.info("%s CLI installed: %s", backend.label, backend.resolve_bin())
    if on_event:
        await on_event("agent_cli_installed", {
            "vendor": backend.vendor,
            "binary": spec.binary,
            "path": backend.resolve_bin() or "",
        })
    return True


def installed_report() -> dict[str, dict]:
    """Which known CLIs are present right now — for health endpoints."""
    report: dict[str, dict] = {}
    for name, spec in _SPECS.items():
        path = shutil.which(name)
        report[name] = {
            "label": spec.label,
            "installed": path is not None,
            "path": path or "",
            "pinned_version": _pinned_version(spec),
            "failed_reason": _failed.get(name, ""),
        }
    return report
