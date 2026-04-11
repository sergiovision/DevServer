"""TaskRun model — individual execution attempts."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    session_id: Mapped[str | None] = mapped_column(String(128))
    branch: Mapped[str | None] = mapped_column(String(256))
    pr_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="started")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    error_log: Mapped[str | None] = mapped_column(Text)
    claude_output: Mapped[str | None] = mapped_column(Text)
    # Interactive-mode plan JSON produced by the plan phase (plan_gate.py).
    plan_json: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    task = relationship("Task", back_populates="runs", lazy="selectin")
