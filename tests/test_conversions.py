"""Settled-payment coordinator: idempotent referral and partner effects."""

from __future__ import annotations

import uuid

import pytest

from pv_growth.attribution.service import attribute
from pv_growth.core.errors import ValidationError
from pv_growth.database.models import (
    AppConfig,
    CommissionEntry,
    FeatureFlag,
    Job,
    Milestone,
    RewardLedger,
)
from pv_growth.events.service import get_or_create_user, ingest
from pv_growth.jobs.runner import run_tick
from pv_growth.partners.service import create_partner
from pv_growth.referrals.service import create_pending_referral

JOB_TYPE = "conversion.settled"


def _user(session, telegram_user_id: int):
    return get_or_create_user(session, telegram_user_id=telegram_user_id)[0]


def _flag(session, key: str, enabled: bool) -> None:
    row = session.get(FeatureFlag, key)
    if row is None:
        session.add(FeatureFlag(key=key, enabled=enabled, note="conversion test"))
    else:
        row.enabled = enabled


@pytest.fixture(autouse=True)
def _conversion_state(session):
    session.query(Job).filter_by(job_type=JOB_TYPE).delete()
    session.query(FeatureFlag).filter(FeatureFlag.key.in_(("REFERRAL_ENABLED", "PARTNER_ENABLED"))).delete(
        synchronize_session=False
    )
    session.query(AppConfig).filter_by(key="referral_policy").delete()
    session.commit()


def test_settled_payment_rewards_referral_and_milestone_exactly_once(session, settings):
    from pv_growth.conversions.service import (
        enqueue_settled_conversion,
        handle_settled_conversion,
    )

    referrer = _user(session, 710001)
    buyer = _user(session, 710002)
    referral, _ = create_pending_referral(session, referrer_id=referrer.id, referred_user_id=buyer.id)
    milestone = Milestone(
        code=f"settled_{uuid.uuid4().hex[:8]}",
        threshold=1,
        reward_kind="days",
        reward_amount=7,
    )
    session.add(milestone)
    session.add(
        AppConfig(
            key="referral_policy",
            value={"reward_kind": "traffic_gb", "reward_amount": 2},
        )
    )
    _flag(session, "REFERRAL_ENABLED", True)
    _flag(session, "PARTNER_ENABLED", False)
    session.commit()

    payment, _ = ingest(
        session,
        "PAYMENT_SUCCESS",
        user_id=buyer.id,
        idempotency_key="pay:referral:710002",
        metadata={"order_ref": "order-ref-1", "amount_cents": 100000},
    )
    job, created = enqueue_settled_conversion(
        session,
        payment_event=payment,
        order_ref="order-ref-1",
        amount_cents=100000,
    )
    duplicate, duplicate_created = enqueue_settled_conversion(
        session,
        payment_event=payment,
        order_ref="order-ref-1",
        amount_cents=100000,
    )
    assert created is True
    assert duplicate_created is False and duplicate.id == job.id

    handle_settled_conversion(session, settings, job.payload)
    handle_settled_conversion(session, settings, job.payload)

    session.refresh(referral)
    assert referral.status == "rewarded"
    rewards = session.query(RewardLedger).filter_by(user_id=referrer.id).all()
    assert sorted((row.source, row.amount) for row in rewards) == [
        ("milestone", 7),
        ("referral", 2),
    ]


def test_partner_conversion_uses_explicit_basis_point_policy(session, settings):
    from pv_growth.conversions.service import (
        enqueue_settled_conversion,
        handle_settled_conversion,
    )

    buyer = _user(session, 720001)
    partner = create_partner(session, name="Sales partner", kind="partner")
    partner.meta = {"commission_bps": 1500}
    source, _ = attribute(session, buyer.id, f"partner_{partner.code}")
    _flag(session, "REFERRAL_ENABLED", False)
    _flag(session, "PARTNER_ENABLED", True)
    session.commit()

    payment, _ = ingest(
        session,
        "PAYMENT_SUCCESS",
        user_id=buyer.id,
        source_id=source.id,
        idempotency_key="pay:partner:720001",
        metadata={"order_ref": "partner-order-1", "amount_cents": 100000},
    )
    job, _ = enqueue_settled_conversion(
        session,
        payment_event=payment,
        order_ref="partner-order-1",
        amount_cents=100000,
    )

    handle_settled_conversion(session, settings, job.payload)

    entry = session.query(CommissionEntry).filter_by(partner_id=partner.id).one()
    assert entry.amount_cents == 100000
    assert entry.commission_cents == 15000


def test_missing_partner_policy_is_retryable_and_never_fabricates_commission(session, settings):
    from pv_growth.conversions.service import (
        enqueue_settled_conversion,
        handle_settled_conversion,
    )

    buyer = _user(session, 730001)
    partner = create_partner(session, name="No policy", kind="partner")
    source, _ = attribute(session, buyer.id, f"partner_{partner.code}")
    _flag(session, "REFERRAL_ENABLED", False)
    _flag(session, "PARTNER_ENABLED", True)
    session.commit()

    payment, _ = ingest(
        session,
        "PAYMENT_SUCCESS",
        user_id=buyer.id,
        source_id=source.id,
        idempotency_key="pay:partner:missing-policy",
        metadata={"order_ref": "partner-order-missing", "amount_cents": 50000},
    )
    job, _ = enqueue_settled_conversion(
        session,
        payment_event=payment,
        order_ref="partner-order-missing",
        amount_cents=50000,
    )
    session.commit()

    with pytest.raises(ValidationError, match="commission_bps"):
        handle_settled_conversion(session, settings, job.payload)
    assert session.query(CommissionEntry).filter_by(partner_id=partner.id).count() == 0

    executed = run_tick(settings, worker_id="conversion-test", batch=1)
    session.expire_all()
    queued = session.get(Job, job.id)
    assert executed == 1
    assert queued.status == "pending"
    assert "commission_bps" in (queued.last_error or "")
    assert session.query(CommissionEntry).filter_by(partner_id=partner.id).count() == 0


def test_non_payment_event_cannot_enqueue_conversion(session):
    from pv_growth.conversions.service import enqueue_settled_conversion

    user = _user(session, 740001)
    event, _ = ingest(
        session,
        "CHECKOUT_STARTED",
        user_id=user.id,
        idempotency_key="checkout:not-settled",
    )

    with pytest.raises(ValidationError, match="PAYMENT_SUCCESS"):
        enqueue_settled_conversion(
            session,
            payment_event=event,
            order_ref="not-settled",
            amount_cents=1000,
        )

    assert session.query(Job).filter_by(job_type=JOB_TYPE).count() == 0
