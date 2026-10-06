from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow

CAMPAIGN_KINDS = (
    "free_config_public",  # public/community config posts
    "free_config_exclusive",  # PV-provisioned exclusive configs
    "purchase",  # purchase CTA campaigns
    "referral",  # referral pushes
    "content",  # scheduled content series
)
CAMPAIGN_STATUSES = ("draft", "active", "paused", "finished")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    start_at: Mapped[datetime | None] = mapped_column(DateTime())
    end_at: Mapped[datetime | None] = mapped_column(DateTime())
    config: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow, onupdate=utcnow)
