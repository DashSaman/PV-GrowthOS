from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow


class ReferralCode(Base):
    """Unique referral code per user (start=ref_<code>)."""

    __tablename__ = "referral_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class Referral(Base):
    """pending → valid (settled qualifying purchase) → rewarded.
    One row per referred user (unique) — duplicate accounts collapse here."""

    __tablename__ = "referrals"
    __table_args__ = (
        UniqueConstraint("referred_user_id", name="uq_referral_referred_user"),
        Index("ix_referral_referrer", "referrer_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    qualifying_order_ref: Mapped[str | None] = mapped_column(String(128))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime())
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class RewardLedger(Base):
    """Immutable reward book. dedupe_key unique — a reward can never pay twice."""

    __tablename__ = "reward_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # referral|milestone|winback
    kind: Mapped[str] = mapped_column(String(16), nullable=False)    # traffic_gb|days|credit
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    meta: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class Milestone(Base):
    """Configurable gamification thresholds (never hardcoded business values)."""

    __tablename__ = "milestones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)  # valid referrals needed
    reward_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reward_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Partner(Base):
    """affiliate | partner | reseller records with tracked links."""

    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="affiliate")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    starts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class CommissionEntry(Base):
    """pending → approved/paid. dedupe_key unique — no double commission."""

    __tablename__ = "commission_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True, nullable=False)
    order_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime())
