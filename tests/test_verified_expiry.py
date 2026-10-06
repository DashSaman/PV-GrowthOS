from datetime import timedelta

from pv_growth.database.models import Campaign, Event, ExclusiveClaim
from pv_growth.database.types import utcnow
from pv_growth.events.service import get_or_create_user
from pv_growth.provisioning import lifecycle_job as sweep


def claim(session, *, future=False):
    user, _ = get_or_create_user(session, telegram_user_id=98765, telegram_chat_id=98765)
    campaign = Campaign(code="expiry", name="Expiry", kind="free_config_exclusive", status="active")
    session.add(campaign)
    session.flush()
    row = ExclusiveClaim(
        claim_key="expiry:1",
        campaign_id=campaign.id,
        user_id=user.id,
        claim_date=utcnow().date(),
        traffic_gb=1,
        validity_hours=24,
        status="active",
        service_ref="growth-expiry",
        expires_at=utcnow() + timedelta(hours=1 if future else -1),
    )
    session.add(row)
    session.commit()
    return row


def test_backend_unavailable_is_not_expiry(session, settings, monkeypatch):
    row = claim(session)
    monkeypatch.setattr(sweep, "get_provisioning", lambda s: None)
    result = sweep.sweep_expired_claims(session, settings)
    assert row.status == "active"
    assert result["expired"] == 0
    assert not session.query(Event).filter_by(event_type="SERVICE_EXPIRED").all()


def test_exhausted_quota_is_detected_before_time_expiry(session, settings, monkeypatch):
    row = claim(session, future=True)

    class Backend:
        def service_state(self, ref):
            return {"exists": True, "enabled": True, "expired": False, "quota_exhausted": True}

    monkeypatch.setattr(sweep, "get_provisioning", lambda s: Backend())
    result = sweep.sweep_expired_claims(session, settings)
    assert result["expired"] == 1
    assert row.status == "expired"
    assert (
        session.query(Event).filter_by(event_type="SERVICE_EXPIRED").one().metadata_json["reason"] == "volume"
    )


def test_lookup_error_does_not_abort_other_claims_or_fabricate_expiry(session, settings, monkeypatch):
    row = claim(session)

    class Backend:
        def service_state(self, ref):
            raise RuntimeError("unknown panel state")

    monkeypatch.setattr(sweep, "get_provisioning", lambda s: Backend())
    assert sweep.sweep_expired_claims(session, settings)["expired"] == 0
    assert row.status == "active"
