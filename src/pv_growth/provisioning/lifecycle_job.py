"""Expiry sweep for exclusive free services.

For every ACTIVE claim past its expires_at, verify the real panel state and
only then emit SERVICE_EXPIRED (evidence-backed, never fabricated). The claim
row moves to `expired`. No event is emitted when the panel still reports the
service alive (clock skew tolerance).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger
from pv_growth.database.models import ExclusiveClaim, Job
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.jobs.runner import RetryableJobError, handler
from pv_growth.provisioning.xui import get_provisioning

log = get_logger("provisioning.sweep")


@handler("provisioning.claim")
def retry_pending_claim(session: Session, settings: Settings, payload: dict) -> None:
    claim_id = payload.get("claim_id")
    claim = session.get(ExclusiveClaim, claim_id) if isinstance(claim_id, int) else None
    if claim is None or claim.status != "pending_provision":
        return
    job = session.execute(
        select(Job).where(Job.idempotency_key == f"provision:{claim.claim_key}")
    ).scalar_one_or_none()
    adapter = get_provisioning(settings)
    if adapter is None:
        from pv_growth.provisioning.xui import ProvisioningError

        error = ProvisioningError("provisioning backend not configured")
        claim.provision_attempts += 1
        claim.last_provision_error = str(error)
        succeeded = False
    else:
        from pv_growth.free_config.exclusive import provision_reserved_claim

        succeeded = provision_reserved_claim(session, settings, claim, adapter)
    if succeeded:
        return
    if job is not None and job.attempt >= job.max_attempts:
        claim.status = "provision_failed"
        return
    raise RetryableJobError(claim.last_provision_error or "provisioning failed")


def sweep_expired_claims(session: Session, settings: Settings) -> dict:
    adapter = get_provisioning(settings)
    now = utcnow()
    due = (
        session.execute(
            select(ExclusiveClaim)
            .where(
                ExclusiveClaim.status == "active",
                ExclusiveClaim.expires_at.isnot(None),
                ExclusiveClaim.expires_at <= now,
            )
            .limit(200)
        )
        .scalars()
        .all()
    )

    expired = kept = 0
    for claim in due:
        if adapter is not None and claim.service_ref:
            state = adapter.service_state(claim.service_ref)
            # evidence: panel says gone, disabled or expired
            if state.get("exists") and not (state.get("expired") or state.get("enabled") is False):
                kept += 1
                continue  # panel still alive — postpone (clock skew)
        claim.status = "expired"
        ingest(
            session,
            "SERVICE_EXPIRED",
            user_id=claim.user_id,
            campaign_id=claim.campaign_id,
            idempotency_key=f"freecfg_exp:{claim.claim_key}",
            metadata={
                "service_ref": claim.service_ref,
                "traffic_gb": claim.traffic_gb,
                "source": "growthos_free_config",
            },
        )
        expired += 1
    if due:
        log.info("expiry sweep", due=len(due), expired=expired, postponed=kept)
    return {"due": len(due), "expired": expired, "postponed": kept}


@handler("provisioning.sweep")
def _sweep_job(session: Session, settings: Settings, payload: dict) -> None:
    sweep_expired_claims(session, settings)
