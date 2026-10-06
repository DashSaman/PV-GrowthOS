from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow

# The 23 normalized event types (spec §10). Ingestion rejects anything else.
EVENT_TYPES = frozenset(
    {
        "BOT_STARTED",
        "SOURCE_ATTRIBUTED",
        "FREE_CONFIG_POSTED",
        "FREE_CONFIG_CLICKED",
        "FREE_CONFIG_CLAIMED",
        "TRIAL_CREATED",
        "TRIAL_CONNECTED",
        "PRICING_VIEWED",
        "CHECKOUT_STARTED",
        "PAYMENT_SUCCESS",
        "SERVICE_CREATED",
        "SERVICE_RENEWED",
        "SERVICE_EXPIRING",
        "SERVICE_EXPIRED",
        "MESSAGE_SENT",
        "MESSAGE_CLICKED",
        "REFERRAL_CREATED",
        "REFERRAL_VALIDATED",
        "REFERRAL_REWARDED",
        "PARTNER_CONVERSION",
        "FEEDBACK_RECEIVED",
        "WINBACK_SUCCESS",
    }
)


class Event(Base):
    """Append-only event log. `idempotency_key` is globally unique — replaying
    the same event (crash, duplicate webhook, double scheduler) is a no-op.

    NEVER store secrets or VPN credentials in `metadata`."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # external uuid
    event_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", Json, nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text)
