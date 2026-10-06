"""Recalculable segments (spec §21). A user's segment is a pure function of
their event history — never hand-set, always recomputable."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from pv_growth.core.logging import get_logger
from pv_growth.database.models import Event, ExclusiveClaim, User
from pv_growth.database.types import utcnow

log = get_logger("segments")

SEGMENTS = (
    "NEW_LEAD",
    "FREE_CONFIG_USER",
    "TRIAL_USER",
    "TRIAL_NO_PURCHASE",
    "HOT_LEAD",
    "FIRST_TIME_BUYER",
    "ACTIVE_CUSTOMER",
    "EXPIRING",
    "EXPIRED",
    "CHURNED",
    "RETURNING_CUSTOMER",
    "VIP",
    "REFERRER",
    "AFFILIATE",
    "RESELLER",
)


def _has_event(session: Session, user_id: int, event_type: str) -> bool:
    return session.execute(
        select(exists().where(Event.user_id == user_id, Event.event_type == event_type))
    ).scalar_one()


def _event_count(session: Session, user_id: int, event_type: str) -> int:
    return session.execute(
        select(func.count())
        .select_from(Event)
        .where(Event.user_id == user_id, Event.event_type == event_type)
    ).scalar_one()


def compute_segment(session: Session, user: User, *, now=None) -> str:
    """Priority-ordered: the most specific actionable state wins."""
    now = now or utcnow()
    uid = user.id

    if _has_event(session, uid, "REFERRAL_REWARDED"):
        return "REFERRER"
    if _event_count(session, uid, "PARTNER_CONVERSION") > 0:
        partner_kind = user.lead_score >= 999  # partners flagged via scoring config
        return "AFFILIATE" if not partner_kind else "RESELLER"

    payments = _event_count(session, uid, "PAYMENT_SUCCESS")
    if payments > 1:
        if _event_count(session, uid, "SERVICE_RENEWED") >= 1:
            return "VIP" if _event_count(session, uid, "SERVICE_RENEWED") >= 2 else "RETURNING_CUSTOMER"
        return "ACTIVE_CUSTOMER"
    if payments == 1:
        return "FIRST_TIME_BUYER"

    # no purchase yet — evaluate the pre-purchase ladder
    trial = _has_event(session, uid, "TRIAL_CREATED")
    connected = _has_event(session, uid, "TRIAL_CONNECTED")
    claimed_free = _has_event(session, uid, "FREE_CONFIG_CLAIMED")
    checkout = _has_event(session, uid, "CHECKOUT_STARTED")
    pricing = _has_event(session, uid, "PRICING_VIEWED")

    if trial and not connected:
        return "TRIAL_NO_PURCHASE"
    if connected and (checkout or pricing):
        return "HOT_LEAD"
    if connected:
        return "TRIAL_USER"
    if claimed_free:
        return "FREE_CONFIG_USER"

    # expiry states (exclusive claims as v1 signal)
    active_claim = session.execute(
        select(ExclusiveClaim).where(ExclusiveClaim.user_id == uid, ExclusiveClaim.status == "active")
    ).scalar_one_or_none()
    if active_claim is not None and active_claim.expires_at:
        if active_claim.expires_at <= now:
            return "EXPIRED"
        if active_claim.expires_at - now <= timedelta(days=3):
            return "EXPIRING"

    started = _has_event(session, uid, "BOT_STARTED")
    if started and user.last_seen_at and now - user.last_seen_at > timedelta(days=30):
        return "CHURNED"
    if started:
        return "NEW_LEAD"
    return "NEW_LEAD"


def _segment_from_prefetched(
    user: User,
    event_counts: dict[str, int],
    active_claim: ExclusiveClaim | None,
    *,
    now,
) -> str:
    def count(event_type: str) -> int:
        return event_counts.get(event_type, 0)

    if count("REFERRAL_REWARDED"):
        return "REFERRER"
    if count("PARTNER_CONVERSION"):
        return "RESELLER" if user.lead_score >= 999 else "AFFILIATE"
    payments = count("PAYMENT_SUCCESS")
    if payments > 1:
        renewals = count("SERVICE_RENEWED")
        if renewals:
            return "VIP" if renewals >= 2 else "RETURNING_CUSTOMER"
        return "ACTIVE_CUSTOMER"
    if payments == 1:
        return "FIRST_TIME_BUYER"

    trial = bool(count("TRIAL_CREATED"))
    connected = bool(count("TRIAL_CONNECTED"))
    claimed_free = bool(count("FREE_CONFIG_CLAIMED"))
    if trial and not connected:
        return "TRIAL_NO_PURCHASE"
    if connected and (count("CHECKOUT_STARTED") or count("PRICING_VIEWED")):
        return "HOT_LEAD"
    if connected:
        return "TRIAL_USER"
    if claimed_free:
        return "FREE_CONFIG_USER"
    if active_claim is not None and active_claim.expires_at:
        if active_claim.expires_at <= now:
            return "EXPIRED"
        if active_claim.expires_at - now <= timedelta(days=3):
            return "EXPIRING"
    if count("BOT_STARTED") and user.last_seen_at:
        if now - user.last_seen_at > timedelta(days=30):
            return "CHURNED"
    return "NEW_LEAD"


def recompute_all_batched(session: Session, *, batch_size: int = 500) -> dict[str, int]:
    """Recompute segments with bounded set queries instead of per-user N+1."""
    counts: dict[str, int] = {}
    effective_batch = max(1, batch_size)
    last_id = 0
    total = 0
    now = utcnow()
    while True:
        users = list(
            session.execute(
                select(User).where(User.id > last_id).order_by(User.id).limit(effective_batch)
            ).scalars()
        )
        if not users:
            break
        user_ids = [user.id for user in users]
        event_rows = session.execute(
            select(Event.user_id, Event.event_type, func.count())
            .where(Event.user_id.in_(user_ids))
            .group_by(Event.user_id, Event.event_type)
        ).all()
        events_by_user: dict[int, dict[str, int]] = {}
        for user_id, event_type, event_count in event_rows:
            events_by_user.setdefault(user_id, {})[event_type] = int(event_count)
        claim_rows = session.execute(
            select(ExclusiveClaim)
            .where(
                ExclusiveClaim.user_id.in_(user_ids),
                ExclusiveClaim.status == "active",
            )
            .order_by(ExclusiveClaim.user_id, ExclusiveClaim.id.desc())
        ).scalars()
        claims_by_user: dict[int, ExclusiveClaim] = {}
        for claim in claim_rows:
            claims_by_user.setdefault(claim.user_id, claim)

        for user in users:
            segment = _segment_from_prefetched(
                user,
                events_by_user.get(user.id, {}),
                claims_by_user.get(user.id),
                now=now,
            )
            user.segment = segment
            counts[segment] = counts.get(segment, 0) + 1
        total += len(users)
        last_id = users[-1].id
    log.info("segments recomputed", users=total)
    return counts


def recompute_all(session: Session, *, batch_size: int = 500) -> dict[str, int]:
    return recompute_all_batched(session, batch_size=batch_size)
