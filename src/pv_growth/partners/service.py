"""Partner/affiliate/reseller tracking + commission ledger.

Distinct from customer referrals: partners get commission on settled orders,
tracked pending → approved. The existing reseller dashboard is NOT rebuilt —
this module only records attribution + commission for later integration."""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.core.logging import get_logger
from pv_growth.database.models import CommissionEntry, Partner
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest

log = get_logger("partners")

PARTNER_KINDS = ("affiliate", "partner", "reseller")


def create_partner(
    session: Session, *, name: str, kind: str = "affiliate", user_id: int | None = None
) -> Partner:
    if kind not in PARTNER_KINDS:
        raise ValidationError(f"unknown partner kind: {kind}")
    partner = Partner(name=name, kind=kind, user_id=user_id, code="pt" + secrets.token_hex(3))
    session.add(partner)
    session.flush()
    return partner


def get_by_code(session: Session, code: str) -> Partner | None:
    return session.execute(select(Partner).where(Partner.code == code)).scalar_one_or_none()


def record_click(session: Session, partner: Partner) -> None:
    partner.clicks += 1


def record_start(session: Session, partner: Partner) -> None:
    partner.starts += 1


def record_conversion(
    session: Session,
    *,
    partner_id: int,
    order_ref: str,
    amount_cents: int,
    commission_cents: int,
) -> tuple[CommissionEntry, bool]:
    """Record a PARTNER_CONVERSION and open a pending commission. Idempotent."""
    dedupe = f"commission:{partner_id}:{order_ref}"
    existing = session.execute(
        select(CommissionEntry).where(CommissionEntry.dedupe_key == dedupe)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    entry = CommissionEntry(
        partner_id=partner_id,
        order_ref=order_ref,
        amount_cents=amount_cents,
        commission_cents=commission_cents,
        dedupe_key=dedupe,
    )
    # commission can be recomputed; store both for audit
    entry.status = "pending"
    session.flush()
    try:
        with session.begin_nested():
            session.add(entry)
            session.flush()
    except IntegrityError:
        return session.execute(
            select(CommissionEntry).where(CommissionEntry.dedupe_key == dedupe)
        ).scalar_one(), False

    partner = session.get(Partner, partner_id)
    if partner is not None:
        partner.orders += 1
    ingest(
        session,
        "PARTNER_CONVERSION",
        user_id=(partner.user_id if partner else None),
        idempotency_key=f"pconv:{dedupe}",
        metadata={
            "partner_id": partner_id,
            "order_ref": order_ref,
            "amount_cents": amount_cents,
            "commission_cents": commission_cents,
        },
    )
    log.info("partner conversion recorded", partner_id=partner_id, order_ref=order_ref)
    return entry, True


def approve_commission(session: Session, entry: CommissionEntry) -> CommissionEntry:
    if entry.status == "paid":
        raise ValidationError("commission already paid")
    entry.status = "approved"
    entry.settled_at = utcnow()
    return entry


def partner_summary(session: Session, partner: Partner) -> dict:
    entries = (
        session.execute(select(CommissionEntry).where(CommissionEntry.partner_id == partner.id))
        .scalars()
        .all()
    )
    resolved = [e for e in entries if e.commission_cents is not None]
    pending = sum(e.commission_cents for e in resolved if e.status == "pending")
    approved = sum(e.commission_cents for e in resolved if e.status in ("approved", "paid"))
    return {
        "code": partner.code,
        "kind": partner.kind,
        "clicks": partner.clicks,
        "starts": partner.starts,
        "orders": partner.orders,
        "pending_commission_cents": pending,
        "approved_commission_cents": approved,
        "unresolved_commission_entries": sum(1 for entry in entries if entry.commission_cents is None),
    }


def handle_bot_start(session: Session, user_id: int, start_param: str | None) -> None:
    """Hook: start=partner_<code> → count click + start."""
    from pv_growth.attribution.service import parse_start_param

    parsed = parse_start_param(start_param)
    if parsed.kind != "partner":
        return
    partner = get_by_code(session, parsed.reference)
    if partner is None:
        return
    record_click(session, partner)
    record_start(session, partner)
