"""AgentMemory model — pgvector embeddings for experience recall."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentMemory(Base):
    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int | None] = mapped_column(Integer)
    task_id: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 768-dim: local fastembed models (services/embeddings.py). The legacy
    # Voyage path was 1536-dim; 001_initial.sql reshapes existing columns.
    embedding = mapped_column(Vector(768))
    memory_type: Mapped[str] = mapped_column(String(32), default="experience")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    # Migration 010 — recency decay + archive lifecycle.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    # Tier 1.2 — supersession: invalidated rows drop out of recall.
    supersedes_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Tier 2.5 — topic / "room" scoping + cross-repo tunnels.
    topic: Mapped[str | None] = mapped_column(String(120), default=None)
