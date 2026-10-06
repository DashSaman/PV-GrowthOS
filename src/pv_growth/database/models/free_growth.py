"""Durable budget reservations and opt-in lottery history."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow


class FreeAllocation(Base):
    __tablename__ = "free_allocations"
    __table_args__ = (CheckConstraint("traffic_bytes > 0", name="ck_free_allocation_positive"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    bucket: Mapped[str] = mapped_column(String(16), index=True)
    traffic_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("exclusive_claims.id"))
    service_ref: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="reserved", nullable=False)
    payload: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class LotteryEntry(Base):
    __tablename__ = "lottery_entries"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id", "draw_date", name="uq_lottery_entry_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    draw_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="entered")
    allocation_id: Mapped[str | None] = mapped_column(ForeignKey("free_allocations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class LotteryDraw(Base):
    __tablename__ = "lottery_draws"
    __table_args__ = (UniqueConstraint("campaign_id", "draw_date", name="uq_lottery_draw_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    draw_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    meta: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
