"""Idempotent downstream effects for newly-settled payments."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import (
    AppConfig,
    AttributionTouch,
    Event,
    Partner,
    Source,
)
from pv_growth.jobs import service as jobs
from pv_growth.jobs.runner import handler
from pv_growth.partners import service as partners
from pv_growth.referrals import service as referrals

JOB_TYPE = "conversion.settled"


def enqueue_settled_conversion(
    session: Session,
    *,
    payment_event: Event,
    order_ref: str,
    amount_cents: int | None,
):
    """Queue effects once per newly-created PAYMENT_SUCCESS event."""
    if payment_event.event_type != "PAYMENT_SUCCESS":
        raise ValidationError("settled conversion requires PAYMENT_SUCCESS")
    if not order_ref:
        raise ValidationError("settled conversion requires order_ref")
    if amount_cents is not None and (
        isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents < 0
    ):
        raise ValidationError("amount_cents must be a non-negative integer when provided")
    return jobs.enqueue(
        session,
        JOB_TYPE,
        {
            "payment_event_id": payment_event.event_id,
            "order_ref": order_ref,
            "amount_cents": amount_cents,
        },
        idempotency_key=f"conversion:{payment_event.idempotency_key}",
        priority=50,
    )


def _referral_policy(session: Session) -> tuple[str, int]:
    config = session.get(AppConfig, "referral_policy")
    value = config.value if config is not None else {}
    kind = value.get("reward_kind") if isinstance(value, dict) else None
    amount = value.get("reward_amount") if isinstance(value, dict) else None
    if (
        not isinstance(kind, str)
        or not kind.strip()
        or isinstance(amount, bool)
        or not isinstance(amount, int)
        or amount <= 0
    ):
        raise ValidationError("referral reward policy is missing or invalid")
    return kind, amount


def _partner_source(session: Session, payment: Event) -> Source | None:
    if payment.source_id is not None:
        source = session.get(Source, payment.source_id)
        if source is not None and source.kind == "partner":
            return source
    if payment.user_id is None:
        return None
    touch = session.execute(
        select(AttributionTouch).where(
            AttributionTouch.user_id == payment.user_id,
            AttributionTouch.kind == "last",
        )
    ).scalar_one_or_none()
    if touch is None:
        return None
    source = session.get(Source, touch.source_id)
    return source if source is not None and source.kind == "partner" else None


def _partner_for_source(session: Session, source: Source) -> Partner:
    prefix = "partner:"
    if not source.code.startswith(prefix) or not source.code[len(prefix) :]:
        raise ValidationError("partner attribution source has no partner code")
    code = source.code[len(prefix) :]
    partner = partners.get_by_code(session, code)
    if partner is None or partner.status != "active":
        raise ValidationError("partner attribution does not resolve to an active partner")
    return partner


def _commission_bps(partner: Partner) -> int:
    meta = partner.meta if isinstance(partner.meta, dict) else {}
    value = meta.get("commission_bps")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise ValidationError("partner commission_bps is missing or invalid")
    return value


@handler(JOB_TYPE)
def handle_settled_conversion(session: Session, settings: Settings, payload: dict) -> None:
    payment = session.execute(
        select(Event).where(Event.event_id == payload.get("payment_event_id"))
    ).scalar_one_or_none()
    if payment is None or payment.event_type != "PAYMENT_SUCCESS":
        raise ValidationError("settled conversion payment event not found")

    order_ref = payload.get("order_ref")
    amount_cents = payload.get("amount_cents")
    if not isinstance(order_ref, str) or not order_ref:
        raise ValidationError("settled conversion order_ref is invalid")

    flags = FlagService(settings)
    if flags.enabled("REFERRAL_ENABLED") and payment.user_id is not None:
        referral = referrals.validate_referral(
            session,
            referred_user_id=payment.user_id,
            order_ref=order_ref,
        )
        if referral is not None:
            if referral.status == "valid":
                reward_kind, reward_amount = _referral_policy(session)
                referrals.reward_referral(
                    session,
                    referral,
                    kind=reward_kind,
                    amount=reward_amount,
                )
            referrals.check_milestones(session, referral.referrer_id)

    if flags.enabled("PARTNER_ENABLED"):
        source = _partner_source(session, payment)
        if source is not None:
            if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents < 0:
                raise ValidationError("partner conversion requires proven amount_cents")
            partner = _partner_for_source(session, source)
            commission_cents = amount_cents * _commission_bps(partner) // 10_000
            partners.record_conversion(
                session,
                partner_id=partner.id,
                order_ref=order_ref,
                amount_cents=amount_cents,
                commission_cents=commission_cents,
            )
