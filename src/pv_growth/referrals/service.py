"""Referral engine — rewards only after a settled qualifying conversion.

Preferred validation flow (spec §17): referred user → starts bot → optional
trial → qualifying purchase → payment settled → referral valid → reward.
Anti-fraud: self-referral blocked; one referral row per referred user; daily
referral rate cap; rewards deduped forever via the ledger."""

from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.core.logging import get_logger
from pv_growth.database.models import (
    Milestone,
    Referral,
    ReferralCode,
    RewardLedger,
)
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest

log = get_logger("referrals")

# anti-farming defaults (configurable via call args / settings later)
MAX_REFERRALS_PER_DAY = 20


def get_or_create_code(session: Session, user_id: int) -> ReferralCode:
    code = session.execute(select(ReferralCode).where(ReferralCode.user_id == user_id)).scalar_one_or_none()
    if code is not None:
        return code
    code = ReferralCode(user_id=user_id, code=_generate_code(session))
    session.add(code)
    session.flush()
    return code


def _generate_code(session: Session) -> str:
    while True:
        candidate = "pv" + secrets.token_hex(3)
        clash = session.execute(
            select(ReferralCode.id).where(ReferralCode.code == candidate)
        ).scalar_one_or_none()
        if clash is None:
            return candidate


def record_referral_click(session: Session, code: ReferralCode) -> None:
    code.clicks += 1


def create_pending_referral(
    session: Session, *, referrer_id: int, referred_user_id: int
) -> tuple[Referral | None, str]:
    """Returns (referral_or_None, reason). Never rewards on click."""
    if referrer_id == referred_user_id:
        return None, "self_referral_blocked"

    existing = session.execute(
        select(Referral).where(Referral.referred_user_id == referred_user_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, "already_referred"

    # anti-farming: cap fresh referrals per referrer per day
    today = date.today()
    today_count = session.execute(
        select(func.count())
        .select_from(Referral)
        .where(
            Referral.referrer_id == referrer_id,
            cast(func.date(Referral.created_at), String) == today.isoformat(),
        )
    ).scalar_one()
    if today_count >= MAX_REFERRALS_PER_DAY:
        return None, "rate_limited"

    referral = Referral(referrer_id=referrer_id, referred_user_id=referred_user_id)
    try:
        with session.begin_nested():
            session.add(referral)
            session.flush()
    except IntegrityError:
        return session.execute(
            select(Referral).where(Referral.referred_user_id == referred_user_id)
        ).scalar_one(), "already_referred"

    ingest(
        session,
        "REFERRAL_CREATED",
        user_id=referrer_id,
        idempotency_key=f"refcreated:{referral.id}",
        metadata={"referred_user_id": referred_user_id},
    )
    log.info("pending referral created", referrer_id=referrer_id, referred_user_id=referred_user_id)
    return referral, "created"


def validate_referral(session: Session, *, referred_user_id: int, order_ref: str) -> Referral | None:
    """Call on PAYMENT_SUCCESS with a SETTLED order. pending → valid.
    Idempotent: re-validation keeps the original validation time."""
    referral = session.execute(
        select(Referral).where(Referral.referred_user_id == referred_user_id)
    ).scalar_one_or_none()
    if referral is None or referral.status != "pending":
        return referral
    referral.status = "valid"
    referral.qualifying_order_ref = order_ref
    referral.validated_at = utcnow()
    ingest(
        session,
        "REFERRAL_VALIDATED",
        user_id=referral.referrer_id,
        idempotency_key=f"refvalid:{referral.id}",
        metadata={"order_ref": order_ref},
    )
    return referral


def grant_reward(
    session: Session,
    *,
    user_id: int,
    source: str,
    kind: str,
    amount: int,
    dedupe_key: str,
    meta: dict | None = None,
) -> tuple[RewardLedger | None, bool]:
    """Ledger append — the single point where rewards pay. Idempotent forever."""
    existing = session.execute(
        select(RewardLedger).where(RewardLedger.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    row = RewardLedger(
        user_id=user_id, source=source, kind=kind, amount=amount, dedupe_key=dedupe_key, meta=meta or {}
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return session.execute(
            select(RewardLedger).where(RewardLedger.dedupe_key == dedupe_key)
        ).scalar_one(), False
    ingest(
        session,
        "REFERRAL_REWARDED",
        user_id=user_id,
        idempotency_key=f"refreward:{dedupe_key}",
        metadata={"source": source, "kind": kind, "amount": amount},
    )
    return row, True


def reward_referral(
    session: Session,
    referral: Referral,
    *,
    kind: str | None = None,
    amount: int | None = None,
) -> bool:
    """valid → rewarded, exactly once."""
    if referral.status != "valid":
        return False
    if (
        not isinstance(kind, str)
        or not kind.strip()
        or isinstance(amount, bool)
        or not isinstance(amount, int)
        or amount <= 0
    ):
        raise ValidationError("referral reward policy is required")
    _, created = grant_reward(
        session,
        user_id=referral.referrer_id,
        source="referral",
        kind=kind,
        amount=amount,
        dedupe_key=f"referral:{referral.id}",
    )
    if created:
        referral.status = "rewarded"
        referral.rewarded_at = utcnow()
    return created


def valid_referral_count(session: Session, user_id: int) -> int:
    return session.execute(
        select(func.count())
        .select_from(Referral)
        .where(
            Referral.referrer_id == user_id,
            Referral.status.in_(("valid", "rewarded")),
        )
    ).scalar_one()


def check_milestones(session: Session, user_id: int) -> list[tuple[Milestone, bool]]:
    """Grant every newly-reached milestone (idempotent via ledger)."""
    count = valid_referral_count(session, user_id)
    granted: list[tuple[Milestone, bool]] = []
    milestones = (
        session.execute(
            select(Milestone).where(Milestone.is_active == 1)  # noqa: E712
        )
        .scalars()
        .all()
    )
    for milestone in milestones:
        if count < milestone.threshold:
            continue
        _, created = grant_reward(
            session,
            user_id=user_id,
            source="milestone",
            kind=milestone.reward_kind,
            amount=milestone.reward_amount,
            dedupe_key=f"milestone:{milestone.code}:{user_id}",
            meta={"threshold": milestone.threshold, "valid_referrals": count},
        )
        granted.append((milestone, created))
    return granted


def handle_bot_start(session: Session, new_user_id: int, start_param: str | None) -> None:
    """Hook from the bot-start flow: start=ref_<code> → pending referral."""
    from pv_growth.attribution.service import parse_start_param

    parsed = parse_start_param(start_param)
    if parsed.kind != "ref":
        return
    code = session.execute(
        select(ReferralCode).where(ReferralCode.code == parsed.reference)
    ).scalar_one_or_none()
    if code is None:
        return
    record_referral_click(session, code)
    create_pending_referral(session, referrer_id=code.user_id, referred_user_id=new_user_id)
