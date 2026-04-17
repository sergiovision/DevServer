"""Application configuration loaded from environment variables."""

import os
from pydantic_settings import BaseSettings

# Resolve .env from project root (3 levels up from this file: src/ -> apps/worker/ -> apps/ -> project root)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_HERE, "..", "..", "..", ".env")


class Settings(BaseSettings):
    # Database
    database_url: str = ""

    # Gitea
    gitea_url: str = ""
    gitea_owner: str = ""
    gitea_token: str = ""

    # Notification channels (zero-config, opt-in via env vars).
    # Each backend is enabled by its own set of env vars — the dispatcher
    # fans notifications out to every configured channel. Leave blank to
    # disable a channel.
    # Telegram — bot token + chat id enable rich notifications + two-way
    # command polling.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Discord — webhook URL (https://discord.com/api/webhooks/...).
    # Supports embeds for colored task-success/failure cards.
    discord_webhook_url: str = ""

    # Claude / Anthropic (primary backend)
    anthropic_api_key: str = ""
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
    google_api_key: str = ""
    gemini_bin: str = "gemini"
    glm_api_key: str = ""  # Zhipu AI (open.bigmodel.cn)
    # glm_bin is not needed — the ``glm`` launcher is always called ``glm``

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

    # PR Preflight
    preflight_ignore_patterns: str = ""  # comma-separated globs, e.g. "*.sqlite,*.sqlite3,data/**"

    # Worker
    worker_host: str = "0.0.0.0"
    worker_port: int = 8000
    worker_concurrency: int = 2

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
        return f"{self.worktree_dir}/.bare"


settings = Settings()
