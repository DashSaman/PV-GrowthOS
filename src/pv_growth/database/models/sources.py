from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, utcnow

# attribution source kinds parsed from `start=` deep links
SOURCE_KINDS = (
    "freecfg",
    "ref",
    "partner",
    "seo",
    "channel",
    "social",
    "socialc",
    "organic",
    "direct",
)


class Source(Base):
    """An acquisition source (deep-link code). Counter fields are denormalized
    conveniences; analytics reconciles against the event log."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="organic")
    medium: Mapped[str | None] = mapped_column(String(64))
    referrer: Mapped[str | None] = mapped_column(String(255))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)

    bot_starts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purchases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    renewal_revenue_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class AttributionTouch(Base):
    """First-touch is IMMUTABLE (unique constraint + service-level guard).
    Last-touch is upserted on every new attribution."""

    __tablename__ = "attribution_touch"
    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_touch_user_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # 'first' | 'last'
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    raw_start_param: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
