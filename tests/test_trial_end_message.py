from pv_growth.database.models import AppConfig, FreeAllocation, MessageLog
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.provisioning import lifecycle_job as sweep
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient
from tests.test_verified_expiry import claim


def setup(session, settings, monkeypatch, *, eligible=True):
    row = claim(session)
    row.status = "expired"
    row.config_payload = {"end_reason": "volume"}
    session.add(AppConfig(key="pv_free_growth_policy", value={"lottery_enabled": True}))
    session.add(
        FreeAllocation(
            id="lottery-test",
            day=row.claim_date,
            bucket="lottery",
            traffic_bytes=1024**3,
            claim_id=row.id,
            user_id=row.user_id,
            campaign_id=row.campaign_id,
            status="delivered",
        )
    )
    session.commit()
    s = settings.model_copy(update={"flag_pv_exclusive_config_enabled": True})
    t = FakeTelegramTransport()
    monkeypatch.setattr("pv_growth.telegram.client.get_telegram", lambda s: TelegramClient(t, s))
    monkeypatch.setattr(
        "pv_growth.free_config.audience.decision_for_user", lambda *a: AudienceDecision(eligible, "test")
    )
    return row, s, t


def test_real_end_sends_once_and_mentions_correct_reason(session, settings, monkeypatch):
    row, s, t = setup(session, settings, monkeypatch)
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    assert len(t.calls) == 1
    assert "حجم" in t.sent_texts()[0]
    assert session.query(MessageLog).one().status == "sent"


def test_recent_buyer_gets_no_trial_end_sales_message(session, settings, monkeypatch):
    row, s, t = setup(session, settings, monkeypatch, eligible=False)
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    assert not t.calls


def test_unknown_delivery_is_never_resent(session, settings, monkeypatch):
    row, s, t = setup(session, settings, monkeypatch)
    t.fail_methods.add("sendMessage")
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    t.fail_methods.clear()
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    assert len(t.calls) == 1
    assert session.query(MessageLog).one().status == "delivery_unknown"


def test_gift_end_sends_the_same_single_purchase_cta(session, settings, monkeypatch):
    row, s, t = setup(session, settings, monkeypatch)
    session.query(FreeAllocation).one().bucket = "gift"
    session.get(AppConfig, "pv_free_growth_policy").value = {"gift_enabled": True}
    session.commit()
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    assert len(t.calls) == 1 and "حجم" in t.sent_texts()[0]


def test_purchase_cta_cooldown_spans_gift_and_lottery_claims(session, settings, monkeypatch):
    from pv_growth.database.models import ExclusiveClaim

    row, s, t = setup(session, settings, monkeypatch)
    sweep.trial_end_job(session, s, {"claim_id": row.id})
    other = ExclusiveClaim(
        claim_key="gift-other",
        campaign_id=row.campaign_id,
        user_id=row.user_id,
        claim_date=row.claim_date,
        status="expired",
        traffic_gb=0,
        traffic_bytes=100 * 1024**2,
        validity_hours=24,
        config_payload={"end_reason": "volume"},
    )
    session.add(other)
    session.flush()
    session.add(
        FreeAllocation(
            id="gift-other",
            day=row.claim_date,
            bucket="gift",
            traffic_bytes=100 * 1024**2,
            claim_id=other.id,
            user_id=row.user_id,
            campaign_id=row.campaign_id,
            status="delivered",
        )
    )
    session.get(AppConfig, "pv_free_growth_policy").value = {"gift_enabled": True, "lottery_enabled": True}
    session.commit()
    import pytest

    from pv_growth.jobs.runner import DeferredJobError

    with pytest.raises(DeferredJobError):
        sweep.trial_end_job(session, s, {"claim_id": other.id})
    assert len(t.calls) == 1


def test_invitation_also_defers_expiry_marketing(session, settings, monkeypatch):
    import pytest

    from pv_growth.database.types import utcnow
    from pv_growth.jobs.runner import DeferredJobError

    row, s, t = setup(session, settings, monkeypatch)
    session.add(
        MessageLog(
            user_id=row.user_id,
            template_code="invite",
            purpose="never_buyer_invite_20261008",
            dedupe_key="invite",
            status="sent",
            sent_at=utcnow(),
        )
    )
    session.commit()
    with pytest.raises(DeferredJobError):
        sweep.trial_end_job(session, s, {"claim_id": row.id})
    assert not t.calls
