from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, utcnow


class FeatureFlag(Base):
    """Runtime feature-flag override; missing rows fall back to env defaults."""

    __tablename__ = "feature_flag"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, onupdate=func.now()
    )
