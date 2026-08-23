"""Environment configuration management API.

Provides endpoints to read, update, and apply .env file settings
through the web UI instead of manual file editing.
"""

import asyncio
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
from config import settings as _settings  # noqa: F401 — used in apply

router = APIRouter(prefix="/internal")

# Path to the .env file (same one Pydantic reads)
_ENV_PATH = Path(config._ENV_PATH).resolve()

# ─── Env var schema ────────────────────────────────────────────────────────
# Each entry describes one known env var with UI metadata.

ENV_SCHEMA: list[dict] = [
    # Paths
    {"key": "DEVSERVER_ROOT", "group": "Paths", "label": "DevServer Root", "type": "path", "secret": False},
    {"key": "WORKTREE_DIR", "group": "Paths", "label": "Worktree Directory", "type": "path", "secret": False},
    {"key": "LOG_DIR", "group": "Paths", "label": "Log Directory", "type": "path", "secret": False},
    # PostgreSQL
    {"key": "DATABASE_URL", "group": "PostgreSQL", "label": "Database URL", "type": "string", "secret": True, "restart": True},
    {"key": "PGHOST", "group": "PostgreSQL", "label": "Host", "type": "string", "secret": False, "restart": True},
    {"key": "PGPORT", "group": "PostgreSQL", "label": "Port", "type": "number", "secret": False, "restart": True},
    {"key": "PGUSER", "group": "PostgreSQL", "label": "User", "type": "string", "secret": False, "restart": True},
    {"key": "PGPASSWORD", "group": "PostgreSQL", "label": "Password", "type": "string", "secret": True, "restart": True},
    {"key": "PGDATABASE", "group": "PostgreSQL", "label": "Database", "type": "string", "secret": False, "restart": True},
    # Docker-only topology hint, consumed by the lifecycle scripts (_lib.sh).
    # "1" = use a PostgreSQL outside the bundled container (host OS / external).
    {"key": "DEVSERVER_HOST_DB", "group": "PostgreSQL", "label": "Use non-bundled DB (docker)", "type": "boolean", "secret": False, "restart": True},
    # Container-perspective DB host/port (docker only). The web/worker CONTAINERS
    # use these to reach Postgres; they differ from the host-side PG* above.
    # Managed automatically by the Database card. Default: postgres:5432 (bundled).
    {"key": "PGHOST_CONTAINER", "group": "PostgreSQL", "label": "Container DB host (docker)", "type": "string", "secret": False, "restart": True},
    {"key": "PGPORT_CONTAINER", "group": "PostgreSQL", "label": "Container DB port (docker)", "type": "number", "secret": False, "restart": True},
    # Git
    {"key": "GIT_SSL_NO_VERIFY", "group": "Git", "label": "SSL No Verify", "type": "boolean", "secret": False},
    {"key": "GIT_USER_EMAIL", "group": "Git", "label": "User Email", "type": "string", "secret": False},
    {"key": "GIT_USER_NAME", "group": "Git", "label": "User Name", "type": "string", "secret": False},
    # Gitea
    {"key": "GITEA_URL", "group": "Gitea", "label": "URL", "type": "url", "secret": False},
    {"key": "GITEA_OWNER", "group": "Gitea", "label": "Owner", "type": "string", "secret": False},
    {"key": "GITEA_TOKEN", "group": "Gitea", "label": "Access Token", "type": "string", "secret": True},
    # Telegram
    {"key": "TELEGRAM_BOT_TOKEN", "group": "Telegram", "label": "Bot Token", "type": "string", "secret": True},
    {"key": "TELEGRAM_CHAT_ID", "group": "Telegram", "label": "Chat ID", "type": "string", "secret": False},
    # Anthropic
    {"key": "ANTHROPIC_API_KEY", "group": "Anthropic", "label": "API Key", "type": "string", "secret": True},
    {"key": "CLAUDE_BIN", "group": "Anthropic", "label": "CLI Binary", "type": "string", "secret": False},
    {"key": "CLAUDE_MAX_TIMEOUT", "group": "Anthropic", "label": "Max Timeout (s)", "type": "number", "secret": False},
    {"key": "CLAUDE_ACTIVITY_TIMEOUT", "group": "Anthropic", "label": "Activity Timeout (s)", "type": "number", "secret": False},
    # OpenAI
    {"key": "OPENAI_API_KEY", "group": "OpenAI", "label": "API Key", "type": "string", "secret": True},
    {"key": "CODEX_BIN", "group": "OpenAI", "label": "Codex Binary", "type": "string", "secret": False},
    {"key": "OPENAI_BASE_URL", "group": "OpenAI", "label": "Base URL (Azure Foundry / proxy)", "type": "url", "secret": False},
    {"key": "OPENAI_API_VERSION", "group": "OpenAI", "label": "API Version (Azure only)", "type": "string", "secret": False},
    # Google (Antigravity CLI — Gemini CLI retired 2026-06-18)
    {"key": "GEMINI_API_KEY", "group": "Google", "label": "Gemini API Key (also used by Antigravity in API mode)", "type": "string", "secret": True},
    {"key": "ANTIGRAVITY_API_KEY", "group": "Google", "label": "Antigravity API Key (optional; defaults to Gemini key / OAuth)", "type": "string", "secret": True},
    # GLM / Zhipu
    {"key": "GLM_API_KEY", "group": "GLM / Zhipu", "label": "API Key", "type": "string", "secret": True},
    # Confluence (external import source). Cloud: URL + email + API token.
    # Data Center: URL + Personal Access Token, username left blank.
    {"key": "CONFLUENCE_URL", "group": "Confluence", "label": "Base URL", "type": "url", "secret": False},
    {"key": "CONFLUENCE_USERNAME", "group": "Confluence", "label": "Username / Email (blank for PAT)", "type": "string", "secret": False},
    {"key": "CONFLUENCE_API_TOKEN", "group": "Confluence", "label": "API Token / PAT", "type": "string", "secret": True},
    # Web UI
    {"key": "NEXT_PUBLIC_WS_URL", "group": "Web UI", "label": "WebSocket URL", "type": "url", "secret": False},
    {"key": "NEXT_PUBLIC_API_URL", "group": "Web UI", "label": "API URL", "type": "url", "secret": False},
    # Worker
    {"key": "WORKER_HOST", "group": "Worker", "label": "Bind Host", "type": "string", "secret": False, "restart": True},
    {"key": "WORKER_PORT", "group": "Worker", "label": "Port", "type": "number", "secret": False, "restart": True},
    {"key": "WORKER_CONCURRENCY", "group": "Worker", "label": "Max Concurrency", "type": "number", "secret": False},
    # Other
    {"key": "PREFLIGHT_IGNORE_PATTERNS", "group": "Other", "label": "Preflight Ignore Patterns", "type": "string", "secret": False},
    {"key": "OBSIDIAN_FOLDER", "group": "Other", "label": "Obsidian Folder", "type": "path", "secret": False},
]


# ─── File I/O helpers ─────────────────────────────────────────────────────

def _parse_env_file() -> dict[str, str]:
    """Parse .env file and return raw key-value pairs."""
    result: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return result
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result


def _write_env_file(updates: dict[str, str]) -> None:
    """Update .env file in-place, preserving comments and structure."""
    if not _ENV_PATH.exists():
        lines = [f"{k}={v}" for k, v in updates.items()]
        _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    original = _ENV_PATH.read_text(encoding="utf-8")
    new_lines: list[str] = []
    updated_keys: set[str] = set()

    for line in original.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            match = re.match(r"^([A-Z_][A-Z0-9_]*)=", stripped)
            if match:
                key = match.group(1)
                if key in updates:
                    val = updates[key]
                    # Quote values that contain spaces
                    if " " in val and not (val.startswith('"') and val.endswith('"')):
                        val = f'"{val}"'
                    new_lines.append(f"{key}={val}")
                    updated_keys.add(key)
                    continue
        new_lines.append(line)

    # Append any new keys not already in the file
    for key, value in updates.items():
        if key not in updated_keys:
            if " " in value and not (value.startswith('"') and value.endswith('"')):
                value = f'"{value}"'
            new_lines.append(f"{key}={value}")

    _ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ─── Endpoints ─────────────────────────────────────────────────────────────

class EnvUpdateRequest(BaseModel):
    variables: dict[str, str]


@router.get("/env")
async def get_env():
    """Return all env variables with their current values and metadata.

    The .env FILE is only one of the two ways this worker is configured, and
    in Docker it is the one that does not exist: compose delivers everything
    through ``env_file:``/``environment:``, which lands in the process
    environment, and the image ships no ``/app/.env``. Reading the file alone
    therefore returned every field blank in a container — which made the
    Database card offer ``127.0.0.1`` and an empty password as its fallback,
    and every "Test connection" fail. So fall back to ``os.environ`` per key.

    ``source`` says which one each value came from, because it decides whether
    saving can stick: a process-env value set by compose outranks the file for
    pydantic, so editing it here is a no-op until the compose env changes too.
    """
    current = _parse_env_file()
    variables = []
    for schema in ENV_SCHEMA:
        key = schema["key"]
        file_value = current.get(key, "")
        env_value = os.environ.get(key, "")
        # An empty file entry is not a value — fall through to the environment.
        value = file_value or env_value
        variables.append({
            **schema,
            "value": value,
            "source": "file" if file_value else ("env" if env_value else ""),
        })
    return {
        "variables": variables,
        "env_path": str(_ENV_PATH),
        "env_file_exists": _ENV_PATH.exists(),
        "in_container": config.in_container(),
    }


@router.put("/env")
async def update_env(req: EnvUpdateRequest):
    """Update env variables in the .env file.

    Reports the keys whose saved value will NOT reach the process, because a
    real environment variable outranks the .env file for pydantic. In Docker
    those are the ones compose sets — the DB coordinates among them — so a save
    that looked successful used to change nothing on the next restart. The UI
    needs to say so rather than imply the switch took.
    """
    try:
        _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_env_file(req.variables)
    except OSError as e:
        # Was a bare 500 with a stack trace, which the wizard rendered as the
        # unhelpful "Failed to save configuration".
        raise HTTPException(
            status_code=500,
            detail=(
                f"Cannot write {_ENV_PATH}: {e.strerror or e}. "
                + (
                    "The worker runs in a container as uid 10001 — point "
                    "DEVSERVER_ENV_FILE at a writable volume path (the compose "
                    "files use /app/config/.env)."
                    if config.in_container()
                    else "Check the file's ownership and permissions."
                )
            ),
        ) from e

    shadowed = [k for k in req.variables if os.environ.get(k)]
    return {
        "success": True,
        "updated": list(req.variables.keys()),
        "env_path": str(_ENV_PATH),
        "shadowed_by_environment": shadowed,
    }


class DbTestRequest(BaseModel):
    """Connection parameters to probe. Either ``database_url`` OR the discrete
    host/port/user/password/database fields. Nothing is persisted — this only
    opens a short-lived connection and reports success/failure.

    ``container_host``/``container_port`` are the same database addressed from
    *inside* a container (the bundled DB's ``postgres`` service name, or
    ``host.docker.internal`` for one on the host OS). The UI knows this mapping
    — it already computes it for ``PGHOST_CONTAINER`` — so it passes it along
    rather than making this endpoint re-derive the topology.
    """
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    database_url: str | None = None
    container_host: str | None = None
    container_port: int | None = None


# Hosts that mean "this machine" — and so mean a *different* machine depending
# on which side of the container boundary you evaluate them from.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _probe_targets(req: DbTestRequest) -> list[tuple[str, int]]:
    """Ordered, de-duplicated (host, port) candidates to try.

    The probe answers "can the worker reach this database?", so it must be
    made from the worker's own vantage point. When the worker is containerised
    the host-side coordinates the UI shows are the wrong ones: 127.0.0.1 is the
    container itself, where nothing listens — which is the whole of the
    ``[Errno 111] Connection refused`` this used to report for every DB mode.
    """
    host = (req.host or "127.0.0.1").strip()
    port = req.port or 5432
    targets: list[tuple[str, int]] = []

    if config.in_container():
        # The caller's container-perspective mapping wins when it gave one.
        if req.container_host:
            targets.append((req.container_host.strip(), req.container_port or port))
        # A loopback host can never be right from in here; the host OS's
        # Postgres is reachable as host.docker.internal (compose grants every
        # tier that alias via extra_hosts).
        if host in _LOOPBACK_HOSTS:
            targets.append(("host.docker.internal", port))
        else:
            targets.append((host, port))
    else:
        targets.append((host, port))

    seen: set[tuple[str, int]] = set()
    return [t for t in targets if not (t in seen or seen.add(t))]


@router.post("/env/test-db")
async def test_db(req: DbTestRequest):
    """Attempt a PostgreSQL connection with the given (unsaved) credentials.

    Runs from the worker process, which is the vantage point that actually
    matters — it's the host that runs tasks and reads/writes the queue. Returns
    ``{ok: True, version, target}`` on success or ``{ok: False, error}`` on
    failure (never raises, so the UI always gets a clean verdict).
    """
    import asyncpg

    async def _connect(**kwargs):
        conn = await asyncio.wait_for(asyncpg.connect(**kwargs), timeout=8)
        try:
            return await conn.fetchval("SELECT version()")
        finally:
            await conn.close()

    if req.database_url:
        try:
            # asyncpg only understands the plain scheme, not the SQLAlchemy
            # ``postgresql+asyncpg://`` variant the worker uses internally.
            dsn = req.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
            return {"ok": True, "version": await _connect(dsn=dsn), "target": "DATABASE_URL"}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Connection timed out after 8s — check host/port and firewall."}
        except Exception as e:  # noqa: BLE001 — surface any driver error verbatim
            return {"ok": False, "error": str(e)}

    targets = _probe_targets(req)
    first_error = ""
    for host, port in targets:
        try:
            version = await _connect(
                host=host,
                port=port,
                user=req.user or "devserver",
                password=req.password or "",
                database=req.database or "devserver",
            )
            return {"ok": True, "version": version, "target": f"{host}:{port}"}
        except asyncio.TimeoutError:
            first_error = first_error or f"Connection to {host}:{port} timed out after 8s — check host/port and firewall."
        except (OSError, asyncpg.CannotConnectNowError) as e:
            # Unreachable — worth trying the next vantage point.
            first_error = first_error or f"{e} (tried {host}:{port})"
        except Exception as e:  # noqa: BLE001 — reached the server; it said no.
            # Authentication and "database does not exist" are answers, not
            # routing problems: another target would fail identically, and
            # retrying only buries the real message.
            return {"ok": False, "error": f"{e} (reached {host}:{port})"}

    error = first_error or "Connection failed."
    if config.in_container() and len(targets) > 1:
        error += (
            " — the worker runs in a container, where 127.0.0.1 is the container itself. "
            'Use the "postgres" service name for the bundled database, or '
            "host.docker.internal for a PostgreSQL on the host OS."
        )
    return {"ok": False, "error": error, "tried": [f"{h}:{p}" for h, p in targets]}


@router.post("/env/apply")
async def apply_env():
    """Reload config from .env file into the running worker process.

    Updates the module-level ``settings`` singleton in-place so that every
    module that imported it via ``from config import settings`` sees the
    new values without a process restart.

    Some settings (database, worker bind address) require a full restart
    to take effect — those are marked ``restart: true`` in the schema.
    """
    try:
        new = config.Settings()
        for field_name in config.Settings.model_fields:
            setattr(config.settings, field_name, getattr(new, field_name))
        return {
            "success": True,
            "message": "Configuration reloaded. Settings marked 'requires restart' need a worker restart.",
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to reload config: {e}")
