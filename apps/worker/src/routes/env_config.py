"""Environment configuration management API.

Provides endpoints to read, update, and apply .env file settings
through the web UI instead of manual file editing.
"""

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
    # Google
    {"key": "GEMINI_API_KEY", "group": "Google", "label": "API Key", "type": "string", "secret": True},
    {"key": "GEMINI_BIN", "group": "Google", "label": "Gemini Binary", "type": "string", "secret": False},
    # GLM / Zhipu
    {"key": "GLM_API_KEY", "group": "GLM / Zhipu", "label": "API Key", "type": "string", "secret": True},
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
    """Return all env variables with their current values and metadata."""
    current = _parse_env_file()
    variables = []
    for schema in ENV_SCHEMA:
        variables.append({
            **schema,
            "value": current.get(schema["key"], ""),
        })
    return {"variables": variables, "env_path": str(_ENV_PATH)}


@router.put("/env")
async def update_env(req: EnvUpdateRequest):
    """Update env variables in the .env file."""
    _write_env_file(req.variables)
    return {"success": True, "updated": list(req.variables.keys())}


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
