"""Phase 4 acceptance: self-referral blocked; duplicate rewards impossible;
rewards only after qualifying conversion; milestones; partner commissions."""

import uuid

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import (
    CommissionEntry,
    Milestone,
    Partner,
    Referral,
    RewardLedger,
)
from pv_growth.events.service import get_or_create_user
from pv_growth.partners import service as partners
from pv_growth.referrals import service as referrals


def _user(session, tg):
    user, _ = get_or_create_user(session, telegram_user_id=tg)
    return user


def test_self_referral_blocked(session):
    referrer = _user(session, 9001)
    referrals.get_or_create_code(session, referrer.id)
    result, reason = referrals.create_pending_referral(
        session, referrer_id=referrer.id, referred_user_id=referrer.id
    )
    assert result is None and reason == "self_referral_blocked"


def test_no_reward_for_click_only(session):
    referrer, friend = _user(session, 9002), _user(session, 9003)
    referrals.get_or_create_code(session, referrer.id)
    referral, reason = referrals.create_pending_referral(
        session, referrer_id=referrer.id, referred_user_id=friend.id
    )
    assert reason == "created" and referral.status == "pending"

    # reward attempt before conversion MUST fail
    assert referrals.reward_referral(session, referral) is False
    assert session.query(RewardLedger).filter_by(user_id=referrer.id).count() == 0


def test_reward_only_after_settled_purchase(session):
    referrer, friend = _user(session, 9010), _user(session, 9011)
    referral, _ = referrals.create_pending_referral(
        session, referrer_id=referrer.id, referred_user_id=friend.id
    )

    validated = referrals.validate_referral(session, referred_user_id=friend.id, order_ref="order-77")
    assert validated.status == "valid"

    first = referrals.reward_referral(session, referral, kind="traffic_gb", amount=2)
    second = referrals.reward_referral(  # duplicate!
        session, referral, kind="traffic_gb", amount=2
    )
    assert first is True and second is False  # acceptance: duplicate rewards impossible
    assert session.query(RewardLedger).filter_by(user_id=referrer.id).count() == 1
    assert referral.status == "rewarded"


def test_valid_referral_reward_requires_explicit_policy_values(session):
    referrer, friend = _user(session, 9012), _user(session, 9013)
    referral, _ = referrals.create_pending_referral(
        session, referrer_id=referrer.id, referred_user_id=friend.id
    )
    referrals.validate_referral(session, referred_user_id=friend.id, order_ref="o-policy")

    with pytest.raises(ValidationError, match="reward policy"):
        referrals.reward_referral(session, referral)

    assert referral.status == "valid"


def test_validate_is_idempotent(session):
    referrer, friend = _user(session, 9020), _user(session, 9021)
    referral, _ = referrals.create_pending_referral(
        session, referrer_id=referrer.id, referred_user_id=friend.id
    )
    referrals.validate_referral(session, referred_user_id=friend.id, order_ref="o1")
    referrals.validate_referral(session, referred_user_id=friend.id, order_ref="o2")
    assert referral.status == "valid"
    assert referral.qualifying_order_ref == "o1"  # first settled order wins


def test_one_referral_row_per_referred_user(session):
    a, b, target = _user(session, 9030), _user(session, 9031), _user(session, 9032)
    r1, reason1 = referrals.create_pending_referral(session, referrer_id=a.id, referred_user_id=target.id)
    r2, reason2 = referrals.create_pending_referral(session, referrer_id=b.id, referred_user_id=target.id)
    assert reason1 == "created"
    assert reason2 == "already_referred" and r2.id == r1.id
    assert session.query(Referral).count() >= 1
    assert session.query(Referral).filter_by(referred_user_id=target.id).count() == 1


def test_milestones_granted_once(session):
    referrer = _user(session, 9040)
    m1 = Milestone(code=f"m1_{uuid.uuid4().hex[:6]}", threshold=1, reward_kind="traffic_gb", reward_amount=3)
    m3 = Milestone(code=f"m3_{uuid.uuid4().hex[:6]}", threshold=3, reward_kind="days", reward_amount=7)
    session.add_all([m1, m3])
    session.flush()

    friend = _user(session, 9041)
    referral, _ = referrals.create_pending_referral(
        session, referrer_id=referrer.id, referred_user_id=friend.id
    )
    referrals.validate_referral(session, referred_user_id=friend.id, order_ref="o1")

    granted = referrals.check_milestones(session, referrer.id)
    granted_map = {m.code: created for m, created in granted}
    assert granted_map[m1.code] is True  # threshold 1 reached
    assert m3.code not in granted_map  # threshold 3 not reached

    again = referrals.check_milestones(session, referrer.id)
    again_map = {m.code: created for m, created in again}
    assert again_map[m1.code] is False  # milestone pays exactly once


def test_bot_start_referral_hook(session):
    referrer = _user(session, 9050)
    code = referrals.get_or_create_code(session, referrer.id)
    new_user = _user(session, 9051)
    referrals.handle_bot_start(session, new_user.id, f"ref_{code.code}")
    assert session.query(Referral).filter_by(referred_user_id=new_user.id).count() == 1
    code_row = session.get(type(code), code.id)
    assert code_row.clicks == 1


def test_partner_commission_flow(session):
    partner = partners.create_partner(session, name="Acme", kind="affiliate")
    entry1, created1 = partners.record_conversion(
        session, partner_id=partner.id, order_ref="po-1", amount_cents=100000, commission_cents=15000
    )
    assert created1 is True and entry1.status == "pending"

    entry2, created2 = partners.record_conversion(  # duplicate -> same entry
        session, partner_id=partner.id, order_ref="po-1", amount_cents=100000, commission_cents=15000
    )
    assert created2 is False and entry2.id == entry1.id
    assert session.query(CommissionEntry).filter_by(partner_id=partner.id).count() == 1
    assert entry1.amount_cents == 100000

    partners.approve_commission(session, entry1)
    summary = partners.partner_summary(session, partner)
    assert summary["approved_commission_cents"] == 15000
    assert summary["pending_commission_cents"] == 0
    assert summary["unresolved_commission_entries"] == 0
    assert summary["orders"] == 1


def test_partner_summary_does_not_guess_legacy_commission(session):
    partner = partners.create_partner(session, name="Legacy", kind="affiliate")
    session.add(
        CommissionEntry(
            partner_id=partner.id,
            order_ref="legacy-order",
            amount_cents=50000,
            commission_cents=None,
            status="approved",
            dedupe_key=f"legacy:{uuid.uuid4().hex}",
        )
    )
    session.flush()

    summary = partners.partner_summary(session, partner)

    assert summary["approved_commission_cents"] == 0
    assert summary["unresolved_commission_entries"] == 1


def test_partner_bot_start_hook(session):
    partner = partners.create_partner(session, name="Hook", kind="partner")
    user = _user(session, 9060)
    partners.handle_bot_start(session, user.id, f"partner_{partner.code}")
    row = session.get(Partner, partner.id)
    assert row.clicks == 1 and row.starts == 1
