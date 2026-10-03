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
from pv_growth.core.errors import NotConfigured, ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.database.models import Campaign, ExclusiveClaim
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
    def create_temp_service(self, *, location: str, traffic_gb: int,
                            validity_hours: int, protocol: str) -> dict: ...


class HttpProvisioningClient:
    """Authorized PV provisioning endpoint (settings-gated)."""

    def __init__(self, settings: Settings) -> None:
        import httpx

        if not settings.provisioning_base_url or not settings.provisioning_token:
            raise NotConfigured("provisioning endpoint not configured (PVG_PROVISIONING_*)")
        self._client = httpx.Client(
            base_url=settings.provisioning_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.provisioning_token}"},
            timeout=settings.provisioning_timeout_seconds,
        )

    def create_temp_service(self, *, location: str, traffic_gb: int,
                            validity_hours: int, protocol: str) -> dict:
        resp = self._client.post("/api/temp-services", json={
            "location": location, "traffic_gb": traffic_gb,
            "validity_hours": validity_hours, "protocol": protocol,
        })
        resp.raise_for_status()
        return resp.json()  # {"service_ref": ..., "config_uri": ...}


class FakeProvisioningClient:
    """Test double; service_ref deterministic per argument tuple."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def create_temp_service(self, *, location: str, traffic_gb: int,
                            validity_hours: int, protocol: str) -> dict:
        payload = {
            "service_ref": f"tmp-{location}-{traffic_gb}gb-{validity_hours}h-{protocol}",
            "config_uri": f"{protocol}://fake@{location}.pv.test:443",
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


def check_eligibility(session: Session, campaign: Campaign, user_id: int,
                      day: date) -> None:
    """Raise ValidationError with a reason when the user may not claim."""
    now = utcnow()
    if not _campaign_window_ok(campaign, now):
        raise ValidationError("campaign not active")

    cfg = {**DEFAULTS, **(campaign.config or {})}

    # per-user-per-day idempotency + per_user_limit over campaign window
    user_claims = session.execute(
        select(func.count()).select_from(ExclusiveClaim).where(
            ExclusiveClaim.campaign_id == campaign.id,
            ExclusiveClaim.user_id == user_id,
        )
    ).scalar_one()
    if user_claims >= int(cfg["per_user_limit"]):
        raise ValidationError("per-user claim limit reached")

    # global quota
    total_claims = session.execute(
        select(func.count()).select_from(ExclusiveClaim).where(
            ExclusiveClaim.campaign_id == campaign.id
        )
    ).scalar_one()
    if total_claims >= int(cfg["max_claims"]):
        raise ValidationError("campaign quota exhausted")
    return None  # claim_key computed for caller below


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

    # idempotent re-claim FIRST: an existing claim for today returns as-is,
    # even if the user has since hit their limit
    claim_key = f"{campaign.id}:{user_id}:{day.isoformat()}"
    existing = session.execute(
        select(ExclusiveClaim).where(ExclusiveClaim.claim_key == claim_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    check_eligibility(session, campaign, user_id, day)

    plan = resolve_day_plan(campaign, day)
    claim = ExclusiveClaim(
        claim_key=claim_key, campaign_id=campaign.id, user_id=user_id,
        claim_date=day, traffic_gb=plan.traffic_gb, validity_hours=plan.validity_hours,
        location=plan.location, status="pending_provision",
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

    # provision via authorized interface; failure leaves claim queued, not lost
    try:
        service = provisioning.create_temp_service(
            location=plan.location, traffic_gb=plan.traffic_gb,
            validity_hours=plan.validity_hours, protocol=plan.protocol,
        )
        claim.service_ref = service.get("service_ref")
        claim.config_payload = {"config_uri": service.get("config_uri")}
        claim.status = "active"
    except Exception as exc:  # noqa: BLE001 — provisioning outage must not kill the claim
        log.error("provisioning failed; claim queued", claim_key=claim_key,
                  error=str(exc))
        claim.status = "pending_provision"

    ingest(session, "FREE_CONFIG_CLAIMED",
           user_id=user_id, campaign_id=campaign.id,
           idempotency_key=f"claim:{claim_key}",
           metadata={"traffic_gb": claim.traffic_gb, "location": claim.location,
                     "status": claim.status})
    ingest(session, "TRIAL_CREATED",
           user_id=user_id, campaign_id=campaign.id,
           idempotency_key=f"trial:{claim_key}",
           metadata={"service_ref": claim.service_ref})
    log.info("exclusive claimed", claim_key=claim_key, status=claim.status)
    return claim, True
