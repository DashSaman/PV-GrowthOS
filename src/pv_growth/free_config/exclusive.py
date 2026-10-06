"""PV exclusive pool: limited free configs provisioned through authorized
PV interfaces. Admin-configurable quotas with per-date overrides — no code
changes required to run a campaign (spec §13).

Flow: channel post → tracked deep link → eligibility → unique temp service →
quota/expiry enforcement → event → follow-up (lifecycle Phase 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.database.models import Campaign, ExclusiveClaim, ProvisioningQuotaLock
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest

log = get_logger("exclusive")

DEFAULTS = {
    "posts_per_day": 1,
    "publish_times": ["12:00"],
    "traffic_gb": 5,
    "validity_hours": 24,
    "location": "auto",
    "fallback_location": "nl",
    "protocol": "vless",
    "max_claims": 100,
    "per_user_limit": 1,
    "date_overrides": {},  # {"2026-10-04": {"traffic_gb": 10}, ...}
}


@dataclass(frozen=True)
class DayPlan:
    traffic_gb: int
    validity_hours: int
    location: str
    protocol: str


class ProvisioningClient(Protocol):
    def create_temp_service(
        self, *, location: str, traffic_gb: int, validity_hours: int, protocol: str
    ) -> dict: ...


# The old placeholder HttpProvisioningClient was removed: the single
# provisioning boundary is pv_growth.provisioning.xui.XUIProvisioningAdapter
# (the same X-UI panel API Mirza itself uses).


class FakeProvisioningClient:
    """Test double; deterministic service identity per idempotency key."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def health(self) -> bool:
        return True  # guard-compatible

    def create_temp_service(
        self,
        *,
        location: str,
        traffic_gb: int,
        validity_hours: int,
        protocol: str,
        idempotency_key: str = "",
        traffic_bytes: int | None = None,
    ) -> dict:
        from pv_growth.provisioning.xui import client_email_for

        payload = {
            "service_ref": client_email_for(idempotency_key or f"{location}:{traffic_gb}"),
            "config_uri": f"{protocol}://fake@{location}.pv.test:443",
            "expiry_ts_ms": 0,
            "replayed": False,
            "traffic_bytes": traffic_bytes if traffic_bytes is not None else traffic_gb * 1024**3,
        }
        self.created.append({**payload, "location": location})
        return payload


def resolve_day_plan(campaign: Campaign, day: date) -> DayPlan:
    """Campaign config + per-date overrides. Admin changes data, not code."""
    cfg = {**DEFAULTS, **(campaign.config or {})}
    override = (cfg.get("date_overrides") or {}).get(day.isoformat(), {})
    cfg.update(override)
    return DayPlan(
        traffic_gb=int(cfg["traffic_gb"]),
        validity_hours=int(cfg["validity_hours"]),
        location=str(cfg["location"]),
        protocol=str(cfg["protocol"]),
    )


def _campaign_window_ok(campaign: Campaign, now: datetime) -> bool:
    if campaign.status != "active":
        return False
    if campaign.start_at and now < campaign.start_at:
        return False
    if campaign.end_at and now > campaign.end_at:
        return False
    return True


def check_eligibility(session: Session, campaign: Campaign, user_id: int, day: date) -> None:
    """Raise ValidationError with a reason when the user may not claim."""
    now = utcnow()
    if not _campaign_window_ok(campaign, now):
        raise ValidationError("campaign not active")

    cfg = {**DEFAULTS, **(campaign.config or {})}

    # per-user-per-day idempotency + per_user_limit over campaign window
    user_claims = session.execute(
        select(func.count())
        .select_from(ExclusiveClaim)
        .where(
            ExclusiveClaim.campaign_id == campaign.id,
            ExclusiveClaim.user_id == user_id,
        )
    ).scalar_one()
    if user_claims >= int(cfg["per_user_limit"]):
        raise ValidationError("per-user claim limit reached")

    # global quota
    total_claims = session.execute(
        select(func.count()).select_from(ExclusiveClaim).where(ExclusiveClaim.campaign_id == campaign.id)
    ).scalar_one()
    if total_claims >= int(cfg["max_claims"]):
        raise ValidationError("campaign quota exhausted")
    return None  # claim_key computed for caller below


def _acquire_quota_locks(session: Session, campaign_id: int, day: date) -> None:
    keys = sorted((f"campaign:{campaign_id}", f"day:{day.isoformat()}"))
    for key in keys:
        if session.get(ProvisioningQuotaLock, key) is not None:
            continue
        try:
            with session.begin_nested():
                session.add(ProvisioningQuotaLock(quota_key=key))
                session.flush()
        except IntegrityError:
            pass
    session.execute(
        select(ProvisioningQuotaLock)
        .where(ProvisioningQuotaLock.quota_key.in_(keys))
        .order_by(ProvisioningQuotaLock.quota_key)
        .with_for_update()
    ).scalars().all()


def provision_reserved_claim(
    session: Session,
    settings: Settings,
    claim: ExclusiveClaim,
    provisioning: ProvisioningClient,
) -> bool:
    """Try one idempotent remote provision; emit TRIAL_CREATED only on proof."""
    from pv_growth.free_config.audience import guard_reserved_claim
    from pv_growth.provisioning.guard import check_backend

    if settings.env == "production":
        claim.status = "audience_blocked"
        claim.last_provision_error = "production grants require the budgeted lottery delivery path"
        return False

    if not guard_reserved_claim(session, settings, claim):
        return False

    claim.provision_attempts += 1
    try:
        check_backend(session, provisioning)
        extra = {"traffic_bytes": claim.traffic_bytes} if claim.traffic_bytes is not None else {}
        service = provisioning.create_temp_service(
            location=claim.location or "auto",
            traffic_gb=claim.traffic_gb,
            validity_hours=claim.validity_hours,
            protocol=str((claim.config_payload or {}).get("protocol") or "vless"),
            idempotency_key=claim.claim_key,
            **extra,
        )
        service_ref = service.get("service_ref")
        config_uri = service.get("config_uri")
        if not service_ref or not config_uri:
            raise ValidationError("provisioning response missing service evidence")
        claim.service_ref = service_ref
        claim.config_payload = {
            **(claim.config_payload or {}),
            "config_uri": config_uri,
            "expiry_ts_ms": service.get("expiry_ts_ms"),
            "replayed": bool(service.get("replayed")),
            "traffic_bytes": service.get("traffic_bytes"),
        }
        claim.status = "active"
        claim.last_provision_error = None
        ingest(
            session,
            "TRIAL_CREATED",
            user_id=claim.user_id,
            campaign_id=claim.campaign_id,
            idempotency_key=f"trial:{claim.claim_key}",
            metadata={"service_ref": claim.service_ref},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — failure is durable retry evidence
        claim.status = "pending_provision"
        claim.last_provision_error = f"{type(exc).__name__}: {exc}"[:1000]
        log.error(
            "provisioning attempt failed",
            claim_key=claim.claim_key,
            attempt=claim.provision_attempts,
            error=claim.last_provision_error,
        )
        return False


def claim_exclusive(
    session: Session,
    settings: Settings,
    flags: FlagService,
    provisioning: ProvisioningClient,
    *,
    campaign_code: str,
    user_id: int,
    day: date | None = None,
) -> tuple[ExclusiveClaim, bool]:
    """Idempotent claim: same user+campaign+day returns the existing claim.
    Raises ValidationError when ineligible. Emits FREE_CONFIG_CLAIMED and
    TRIAL_CREATED exactly once (idempotency keys)."""
    if not flags.enabled("FREE_CONFIG_ENABLED") or not flags.enabled("PV_EXCLUSIVE_CONFIG_ENABLED"):
        raise ValidationError("exclusive free config disabled")

    from pv_growth.campaigns.service import get_by_code

    day = day or date.today()
    campaign = get_by_code(session, campaign_code)
    if campaign is None or campaign.kind != "free_config_exclusive":
        raise ValidationError("unknown exclusive campaign")
    if settings.env == "production":
        raise ValidationError("دریافت تست شخصی فقط از مسیر قرعه‌کشی روزانه انجام می‌شود.")
    if (campaign.config or {}).get("lottery") or campaign.code == "pv_daily_lottery":
        raise ValidationError("lottery campaigns require an opt-in draw")

    from pv_growth.free_config.audience import require_free_audience

    require_free_audience(session, settings, user_id)

    # idempotent re-claim FIRST: an existing claim for today returns as-is,
    # even if the user has since hit their limit
    claim_key = f"{campaign.id}:{user_id}:{day.isoformat()}"
    existing = session.execute(
        select(ExclusiveClaim).where(ExclusiveClaim.claim_key == claim_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    _acquire_quota_locks(session, campaign.id, day)
    from pv_growth.provisioning.guard import claims_today

    used_today = claims_today(session, day)
    if used_today >= settings.free_daily_budget:
        raise ValidationError(f"daily free budget exhausted ({used_today}/{settings.free_daily_budget})")
    check_eligibility(session, campaign, user_id, day)

    plan = resolve_day_plan(campaign, day)
    claim = ExclusiveClaim(
        claim_key=claim_key,
        campaign_id=campaign.id,
        user_id=user_id,
        claim_date=day,
        traffic_gb=plan.traffic_gb,
        validity_hours=plan.validity_hours,
        location=plan.location,
        status="pending_provision",
        config_payload={"protocol": plan.protocol},
        expires_at=utcnow() + timedelta(hours=plan.validity_hours),
    )
    try:
        with session.begin_nested():
            session.add(claim)
            session.flush()
    except IntegrityError:
        existing = session.execute(
            select(ExclusiveClaim).where(ExclusiveClaim.claim_key == claim_key)
        ).scalar_one()
        return existing, False

    provision_reserved_claim(session, settings, claim, provisioning)
    if claim.status == "pending_provision":
        from pv_growth.jobs.service import enqueue

        enqueue(
            session,
            "provisioning.claim",
            {"claim_id": claim.id},
            idempotency_key=f"provision:{claim.claim_key}",
            max_attempts=settings.job_max_attempts_default,
            priority=60,
        )

    ingest(
        session,
        "FREE_CONFIG_CLAIMED",
        user_id=user_id,
        campaign_id=campaign.id,
        idempotency_key=f"claim:{claim_key}",
        metadata={"traffic_gb": claim.traffic_gb, "location": claim.location, "status": claim.status},
    )
    log.info("exclusive claimed", claim_key=claim_key, status=claim.status)
    return claim, True
