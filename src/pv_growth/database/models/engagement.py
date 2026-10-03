from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow

CONTENT_STATES = ("draft", "validated", "scheduled", "published", "failed")
CONTENT_KINDS = ("free_public_config", "pv_exclusive", "education", "utility",
                 "purchase_cta", "referral", "service_status")


class ContentItem(Base):
    """Content moving draft → validated → scheduled → published/failed.
    Commercial claims live in `facts` and are validated against campaign data
    BEFORE a transition to validated is allowed."""

    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_content_dedupe"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="education")
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft",
                                        index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(128))
    campaign_code: Mapped[str | None] = mapped_column(String(64))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime())
    published_at: Mapped[datetime | None] = mapped_column(DateTime())
    message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow,
                                                 onupdate=utcnow)


class FeedbackRating(Base):
    """1–5 rating after a suitable usage period. Testimonials publish only
    with stored consent (spec §22)."""

    __tablename__ = "feedback_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..5
    comment: Mapped[str | None] = mapped_column(Text)
    testimonial_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class CompetitorSource(Base):
    """Admin-configured public source watched for specific field changes.
    Never an uncontrolled crawler — one URL, one parser spec, one schedule."""

    __tablename__ = "competitor_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="http_text")
    # fields: {"price": {"regex": "..."}, "trial_gb": {"regex": "..."}}
    fields: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_snapshot: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class CompetitorChange(Base):
    """Structured change record: field, old, new, observed_at, source url."""

    __tablename__ = "competitor_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("competitor_sources.id"), index=True)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(255))
    new_value: Mapped[str | None] = mapped_column(String(255))
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    source_url: Mapped[str | None] = mapped_column(String(512))
    observed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
