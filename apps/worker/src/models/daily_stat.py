"""DailyStat model — aggregated daily metrics."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, Date, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DailyStat(Base):
    __tablename__ = "daily_stats"

    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    total_duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    total_turns: Mapped[int] = mapped_column(Integer, default=0)
