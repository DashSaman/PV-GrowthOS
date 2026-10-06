from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow


class ConfigSource(Base):
    """Admin-maintained registry of APPROVED public sources only.
    Public/community configs are always labeled as such when published."""

    __tablename__ = "config_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))  # github_file | telegram_channel
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512))  # raw file URL or channel handle
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(String(255))

    # reliability metrics (persisted; weak sources lose ranking weight)
    fetch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime())

    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class RawConfig(Base):
    """A parsed public config candidate moving through the staged pipeline.
    raw_uri is required for publishing public configs (they are public by
    nature) but must never appear in logs or event metadata."""

    __tablename__ = "raw_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("config_sources.id"), index=True)
    uri_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    protocol: Mapped[str] = mapped_column(String(16))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    raw_uri: Mapped[str] = mapped_column(Text, nullable=False)
    remark: Mapped[str | None] = mapped_column(String(255))

    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(128))
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    health_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    health_ok: Mapped[bool | None] = mapped_column(Boolean)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="fetched", index=True
    )  # fetched|validated|published|rejected|stale
    fetched_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime())


class PublishedPost(Base):
    """Idempotency guard for channel publishing: one row per dedupe_key.
    Two scheduler runs racing on the same slot cannot double-post."""

    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="free_public")
    config_id: Mapped[int | None] = mapped_column(ForeignKey("raw_configs.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    message_id: Mapped[int | None] = mapped_column(Integer)
    posted_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class ExclusiveClaim(Base):
    """A user's claim of a PV-exclusive free config (temporary service).

    claim_key = campaign:user:date — unique: per-user per-day claims are
    idempotent and per-user limits are enforceable."""

    __tablename__ = "exclusive_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    claim_date: Mapped[date] = mapped_column(Date, nullable=False)

    traffic_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validity_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    location: Mapped[str | None] = mapped_column(String(64))
    service_ref: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_provision")
    config_payload: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    provision_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_provision_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime())


class ProvisioningQuotaLock(Base):
    """Rows locked transactionally while reserving campaign/day quota."""

    __tablename__ = "provisioning_quota_locks"

    quota_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
