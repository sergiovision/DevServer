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

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

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
