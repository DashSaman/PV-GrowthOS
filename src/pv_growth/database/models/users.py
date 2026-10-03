from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, utcnow


class User(Base):
    """Contact identity. Mirza remains the source of truth for purchases;
    GrowthOS users mirror bot-side identities keyed by telegram id."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(128))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mirza_user_ref: Mapped[str | None] = mapped_column(String(128), index=True)

    # derived state (recomputable from events — segments/scoring services own these)
    lead_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    segment: Mapped[str | None] = mapped_column(String(32), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
