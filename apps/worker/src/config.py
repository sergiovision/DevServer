"""Application configuration loaded from environment variables."""

import os
from pydantic_settings import BaseSettings

# Resolve .env from project root (3 levels up from this file: src/ -> apps/worker/ -> apps/ -> project root)
#
# That walk is only correct in a CHECKOUT. The image flattens the tree to
# /app/src, where three levels up is "/" — so the worker was reading, and the
# Settings/Setup save was WRITING, container root. Root owns it and the worker
# runs as uid 10001, so every save died with PermissionError on '/.env'.
# DEVSERVER_ENV_FILE names the file outright; DEVSERVER_ROOT (set to /app by
# every compose file) anchors it otherwise.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.environ.get("DEVSERVER_ENV_FILE") or os.path.join(
    os.environ.get("DEVSERVER_ROOT") or os.path.join(_HERE, "..", "..", ".."),
    ".env",
)


def in_container() -> bool:
    """True when this process runs inside a container.

    Two things need it and must agree: the Ask Agent action runner (which
    refuses to restart a host it isn't on) and the Database card's connection
    probe. A container's ``127.0.0.1`` is not the host's, so any check that
    reports "will this connect?" is wrong unless it knows which side it is on.
    """
    return os.path.exists("/.dockerenv") or os.environ.get("DEVSERVER_IN_DOCKER") == "1"


class Settings(BaseSettings):
    # Database
    database_url: str = ""

    # Gitea
    gitea_url: str = ""
    gitea_owner: str = ""
    gitea_token: str = ""

    # GitHub — optional global fallback token used only by repos whose
    # provider is 'github' and that do not carry their own per-repo token.
    # The Gitea token is deliberately NOT reused for GitHub repos: GitHub
    # would reject it with a misleading "Invalid username or token" error.
    github_token: str = ""

    # Notification channels (zero-config, opt-in via env vars).
    # Each backend is enabled by its own set of env vars — the dispatcher
    # fans notifications out to every configured channel. Leave blank to
    # disable a channel.
    # Telegram — bot token + chat id enable rich notifications + two-way
    # command polling.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Command polling on/off for THIS instance. Telegram's getUpdates is
    # exclusive per bot token: a second poller on the same token terminates
    # the first and both then flap with HTTP 409. sendMessage has no such
    # limit, so an instance with polling disabled still delivers every
    # notification — it just stops competing for the command channel.
    # Set false on every instance but one when several share a bot token.
    telegram_polling_enabled: bool = True
    # Discord — webhook URL (https://discord.com/api/webhooks/...).
    # Supports embeds for colored task-success/failure cards.
    discord_webhook_url: str = ""

    # Claude / Anthropic (primary backend)
    anthropic_api_key: str = ""
    # CLI binary name (resolved on PATH) or an absolute path to it. On Windows
    # the npm shim is ``claude.CMD``; set this to its full path (e.g.
    # ``C:\Users\you\AppData\Roaming\npm\claude.CMD``) when the worker process
    # does not have the npm global bin dir on PATH.
    claude_bin: str = "claude"
    claude_max_timeout: int = 3600

    # Alternative agent backends (wingmen). Each is optional — the task-form
    # combobox lets the user pick which vendor a task runs on, but any
    # missing binary or API key just means that vendor fails at subprocess
    # spawn time. Only Anthropic is production-tested today.
    openai_api_key: str = ""
    codex_bin: str = "codex"
    # Optional OpenAI-compatible endpoint override. Set to an Azure OpenAI
    # / Azure AI Foundry URL (e.g. https://<resource>.openai.azure.com/)
    # to route Codex through Azure instead of api.openai.com. When set,
    # the worker propagates it into the Codex subprocess env.
    openai_base_url: str = ""
    # Only needed for Azure OpenAI / Azure AI Foundry — the API version
    # query param Azure requires (e.g. "2024-10-01-preview").
    openai_api_version: str = ""
    gemini_api_key: str = ""  # Gemini *API* (system-LLM HTTP path) — still live
    # The Gemini CLI was retired 2026-06-18; the Google agent backend now
    # drives the Antigravity CLI, always invoked as ``agy`` (hardcoded in
    # AntigravityBackend, like every other backend's binary). Kept only so the
    # legacy GEMINI_BIN setting still parses.
    gemini_bin: str = "agy"
    glm_api_key: str = ""  # Zhipu AI (open.bigmodel.cn)
    # glm_bin is not needed — the ``glm`` launcher is always called ``glm``

    # ── Lazy agent-CLI installation ─────────────────────────────────────
    # The image no longer bakes the vendor CLIs in: claude (316 MB), codex
    # (259 MB) and agy (196 MB) were 771 MB of a 1.9 GB image, for four
    # vendors of which a deployment typically uses one. Each is fetched by
    # services/agent_cli_installer.py the first time a task selects that
    # vendor, into ``agent_cli_dir`` — a persistent volume in Docker, so the
    # download happens once per deployment, not once per container start.
    #
    # Set AGENT_CLI_AUTO_INSTALL=false for an air-gapped deployment and
    # pre-seed the volume from a machine that has egress.
    # Empty ``agent_cli_dir`` also disables installing, which is the default
    # for a host install where the operator manages the CLIs themselves.
    agent_cli_dir: str = ""
    agent_cli_auto_install: bool = True
    # Version pins, moved here from the Dockerfile ARGs when the installs
    # became a runtime concern. Same intent as before: a vendor's breaking
    # release must not be able to break every deployment at once. Bump
    # deliberately, after testing.
    claude_code_version: str = "2.1.237"
    codex_version: str = "0.148.0"
    # The Antigravity release channel serves exactly one version and offers no
    # way to request an older one, so this is an *assertion*, not a pin: set it
    # and the install fails loudly once the channel moves. Empty accepts
    # whatever the channel currently serves.
    agy_version: str = ""

    # Local embeddings (semantic memory recall). Fully local via fastembed
    # (ONNX runtime, CPU) — no cloud API, no key. This replaces the old
    # Voyage AI HTTP path. The vector dimension is fixed at the
    # ``agent_memory.embedding`` column DDL (vector(768)), so swapping this
    # to a model with a different dimension requires a schema migration +
    # re-embed (scripts/reembed_memory.py). The default is general-purpose
    # and 768-dim; ``jinaai/jina-embeddings-v2-base-code`` is a code-aware
    # 768-dim alternative.
    embedding_model: str = "BAAI/bge-base-en-v1.5"

    # Paths
    devserver_root: str = ""
    worktree_dir: str = ""
    log_dir: str = ""

    # Git
    git_ssl_no_verify: bool = False
    git_user_email: str = ""
    git_user_name: str = ""

    # Obsidian
    obsidian_folder: str = ""  # Absolute path to Obsidian vault folder for plan exports

    # Confluence (external import source). Global defaults — a repo can carry
    # its own confluence_url/confluence_username/confluence_token overrides.
    # Cloud: base URL like https://yourorg.atlassian.net (the /wiki suffix is
    # added automatically) + account email + API token (Basic auth).
    # Data Center/Server: base URL + a Personal Access Token with the
    # username left blank (Bearer auth).
    confluence_url: str = ""
    confluence_username: str = ""
    confluence_api_token: str = ""

    # PR Preflight
    preflight_ignore_patterns: str = ""  # comma-separated globs, e.g. "*.sqlite,*.sqlite3,data/**"

    # Worker
    worker_host: str = "0.0.0.0"
    worker_port: int = 8000
    worker_concurrency: int = 2

    # Human-readable name for THIS deployment. Several DevServer instances on
    # a network commonly share one Telegram chat (and sometimes one bot), so
    # every outbound notification is tagged with this to say which machine it
    # came from, and commands can be aimed at one instance with `/cmd@name`.
    # Defaults to the host's short name.
    instance_name: str = ""

    # Shared secret for the worker endpoints that *execute* something on the
    # host — today the Ask Agent panel's approved-action runner. The web tier
    # attaches it as X-Internal-Token. Optional (unset = unenforced, matching
    # every pre-existing /internal route) but strongly recommended: the worker
    # binds 0.0.0.0 by default and has no authentication of its own, so the
    # port must never be exposed regardless.
    internal_api_token: str = ""

    # Web UI — port the Next.js dashboard listens on. The worker uses this
    # to reach the Next.js enqueue / worker API on localhost.
    web_port: int = 3200

    # Base URL the worker uses to reach the web tier. Blank = localhost on
    # web_port, which is right for a same-host install but WRONG in Docker,
    # where the two tiers are separate containers and the worker's localhost
    # is itself: every enqueue failed with httpx.ConnectError, so
    # /internal/tasks/create returned enqueued=false and nothing ever ran.
    # Compose sets this to http://web:3200 (the service name on the shared
    # network). Trailing slashes are trimmed by web_base_url.
    web_url: str = ""

    model_config = {"env_file": _ENV_PATH, "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def asyncpg_url(self) -> str:
        """Return the database URL with asyncpg driver."""
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def bare_repo_dir(self) -> str:
        return os.path.join(self.worktree_dir, ".bare")

    @property
    def web_base_url(self) -> str:
        """Base URL of the web tier, without a trailing slash."""
        if self.web_url.strip():
            return self.web_url.strip().rstrip("/")
        return f"http://localhost:{self.web_port}"

    @property
    def instance_label(self) -> str:
        """This deployment's name, falling back to the host's short name.

        Used to tag Telegram/Discord notifications and to route `/cmd@name`
        at one instance when several share a chat.
        """
        if self.instance_name.strip():
            return self.instance_name.strip()
        import socket

        return socket.gethostname().split(".")[0] or "devserver"


settings = Settings()
