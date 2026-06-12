"""Repo model — repository configuration."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    gitea_url: Mapped[str] = mapped_column(String(512), nullable=False)
    gitea_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    gitea_repo: Mapped[str] = mapped_column(String(128), nullable=False)
    clone_url: Mapped[str] = mapped_column(String(512), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(64), default="main")
    build_cmd: Mapped[str] = mapped_column(Text, default="")
    test_cmd: Mapped[str] = mapped_column(Text, default="")
    lint_cmd: Mapped[str] = mapped_column(Text, default="")
    pre_cmd: Mapped[str] = mapped_column(Text, default="")
    claude_model: Mapped[str] = mapped_column(String(32), default="sonnet")
    claude_allowed_tools: Mapped[str] = mapped_column(Text, default="Read,Write,Edit,Glob,Grep,Bash")
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    timeout_minutes: Mapped[int] = mapped_column(Integer, default=60)
    claude_md_path: Mapped[str] = mapped_column(String(256), default="CLAUDE.md")
    gitea_token: Mapped[str] = mapped_column(String(256), default="")
    # Git host provider: "gitea" (default/legacy), "github", or "local".
    # Drives the clone-URL auth scheme and the pull-request REST API in
    # git_ops. "local" means a folder on the worker host (path stored in
    # gitea_url — the "Local Root Folder"): no clone, no worktree copy, no
    # push, no PR; the agent runs git directly inside the folder.
    provider: Mapped[str] = mapped_column(String(16), default="gitea", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    tasks = relationship("Task", back_populates="repo", lazy="selectin")
