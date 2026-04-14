"""Task model — coding tasks parsed from backlogs."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(Integer, ForeignKey("repos.id"), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    acceptance: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    mode: Mapped[str] = mapped_column(String(16), default="autonomous")
    status: Mapped[str] = mapped_column(String(24), default="pending")
    depends_on: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list)
    queue_job_id: Mapped[str | None] = mapped_column(String(128))
    skip_verify: Mapped[bool] = mapped_column(Boolean, default=False)
    claude_mode: Mapped[str] = mapped_column(String(8), default="max")
    # Phase 2+ — AgentBackend abstraction (migration 006).
    # 'anthropic' | 'google' | 'openai' | 'qwen'. Defaults to 'anthropic' so
    # every existing task keeps running on Claude Code CLI unchanged.
    agent_vendor: Mapped[str] = mapped_column(String(16), default="anthropic")
    claude_model: Mapped[str | None] = mapped_column(String(32), default=None)
    max_turns: Mapped[int | None] = mapped_column(Integer, default=None)
    # Phase 2 — per-task budget circuit breaker (services/agent_runner.py).
    # max_cost_usd is only meaningful in API mode; Max mode reports 0 and is skipped.
    # max_wall_seconds bounds the sum of Claude + verifier wall time across retries.
    # Both NULL = no limit (inherits from worker config).
    max_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), default=None)
    max_wall_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    created_by: Mapped[str] = mapped_column(String(64), default="ui")
    # Interactive-mode plan gate (Phase 1 — plan_gate.py).
    # Set by POST /api/tasks/[id]/approve (or /reject) from the dashboard.
    plan_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    plan_rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    git_flow: Mapped[str] = mapped_column(String(16), default="branch")
    backup_vendor: Mapped[str | None] = mapped_column(String(16), default=None)
    backup_model: Mapped[str | None] = mapped_column(String(32), default="claude-sonnet-4-6")
    is_continuation: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    repo = relationship("Repo", back_populates="tasks", lazy="selectin")
    runs = relationship("TaskRun", back_populates="task", lazy="selectin")
