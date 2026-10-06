"""Campaign domain helpers: lookup, active-window checks, code generation."""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import Campaign
from pv_growth.database.types import utcnow


def get_by_code(session: Session, code: str) -> Campaign | None:
    return session.execute(select(Campaign).where(Campaign.code == code)).scalar_one_or_none()


def require_by_code(session: Session, code: str) -> Campaign:
    campaign = get_by_code(session, code)
    if campaign is None:
        raise ValidationError(f"unknown campaign: {code}")
    return campaign


def is_active(campaign: Campaign, *, now=None) -> bool:
    now = now or utcnow()
    if campaign.status != "active":
        return False
    if campaign.start_at and now < campaign.start_at:
        return False
    if campaign.end_at and now > campaign.end_at:
        return False
    return True


def list_active(session: Session, kind: str | None = None) -> list[Campaign]:
    query = select(Campaign).where(Campaign.status == "active")
    if kind:
        query = query.where(Campaign.kind == kind)
    return list(session.execute(query).scalars().all())


def generate_code(prefix: str = "camp") -> str:
    return f"{prefix}_{secrets.token_hex(3)}"
