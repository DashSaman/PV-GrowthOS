from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow

JOB_STATUSES = ("pending", "running", "done", "failed", "cancelled")


class Job(Base):
    """Durable PostgreSQL-backed job (no Redis in v1). Jobs survive restarts;
    claiming is an atomic row UPDATE so duplicate schedulers can't double-run;
    `idempotency_key` prevents duplicate enqueue of the same logical work."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_due", "status", "scheduled_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime())
    last_error: Mapped[str | None] = mapped_column(Text)

    locked_at: Mapped[datetime | None] = mapped_column(DateTime())
    locked_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime())


class MessageTemplate(Base):
    """Message templates by intent. Bodies contain {{variables}} only —
    commercial facts (price/quota/…) are injected from structured data."""

    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    intent: Mapped[str] = mapped_column(String(32), index=True)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="fa")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    cta_label: Mapped[str | None] = mapped_column(String(64))
    cta_url: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class MessageLog(Base):
    """Every outbound customer message. `dedupe_key` unique — the effect-level
    guard that makes double scheduler runs harmless."""

    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    template_code: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64), index=True)  # rule/campaign code
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    send_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    meta: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime())


class LifecycleRule(Base):
    """Rule-based automation. All knobs are data (admin-editable, no deploys).

    trigger: state key evaluated by the scan (see lifecycle/triggers.py)
    conditions: {"min_lead_score": 50, "segment": "HOT_LEAD"} optional filters
    stop_conditions: event types that must abort the send (e.g. PAYMENT_SUCCESS)"""

    __tablename__ = "lifecycle_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    trigger: Mapped[str] = mapped_column(String(64))
    delay_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conditions: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    template_code: Mapped[str] = mapped_column(String(64))
    cta_kind: Mapped[str | None] = mapped_column(String(32))
    cooldown_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    max_sends: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    stop_conditions: Mapped[dict] = mapped_column(Json, nullable=False, default=list)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
