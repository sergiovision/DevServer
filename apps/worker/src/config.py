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

    # Telegram Controller (external vs local)
    telegram_controller_mode: str = "external"  # "external" or "local"
    telegram_controller_url: str = "http://127.0.0.1:8200"
    telegram_controller_token: str = ""

    # Claude / Anthropic
    anthropic_api_key: str = ""
    claude_bin: str = "claude"
    claude_max_timeout: int = 3600

    # Paths
    devserver_root: str = ""
    worktree_dir: str = ""
    log_dir: str = ""

    # Git
    git_ssl_no_verify: bool = False
    git_user_email: str = ""
    git_user_name: str = ""

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
