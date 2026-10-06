"""Analytics queries: funnels, sources, cohorts, revenue — plain SQL over
the event log (no heavy analytics platform). Every number reconciles with
events; tests assert exactly that."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pv_growth.database.models import (
    AttributionTouch,
    Event,
    Referral,
    Source,
    User,
)

FUNNEL_STEPS = (
    "BOT_STARTED",
    "TRIAL_CREATED",
    "TRIAL_CONNECTED",
    "PRICING_VIEWED",
    "CHECKOUT_STARTED",
    "PAYMENT_SUCCESS",
)


def funnel(session: Session, *, since=None) -> dict:
    """Distinct users per funnel step, in order."""
    out: dict[str, int] = {}
    for step in FUNNEL_STEPS:
        query = select(func.count(func.distinct(Event.user_id))).where(Event.event_type == step)
        if since is not None:
            query = query.where(Event.occurred_at >= since)
        out[step] = session.execute(query).scalar_one()
    return out


def conversion_rate(session: Session) -> float:
    f = funnel(session)
    starts = f["BOT_STARTED"] or 0
    if starts == 0:
        return 0.0
    return round(f["PAYMENT_SUCCESS"] / starts, 4)


def source_performance(session: Session) -> list[dict]:
    """Per-source: starts, trials, purchases, revenue — reconciles with events."""
    sources = session.execute(select(Source)).scalars().all()
    rows: list[dict] = []
    for source in sources:
        starts = session.execute(
            select(func.count(func.distinct(Event.user_id))).where(
                Event.source_id == source.id, Event.event_type == "SOURCE_ATTRIBUTED"
            )
        ).scalar_one()
        # purchases & revenue attributed via first-touch of users of this source
        purchases = 0
        revenue = 0
        user_ids = (
            session.execute(
                select(AttributionTouch.user_id).where(
                    AttributionTouch.source_id == source.id, AttributionTouch.kind == "first"
                )
            )
            .scalars()
            .all()
        )
        for uid in user_ids:
            pay_events = (
                session.execute(
                    select(Event).where(Event.user_id == uid, Event.event_type == "PAYMENT_SUCCESS")
                )
                .scalars()
                .all()
            )
            purchases += len({e.user_id for e in pay_events}) and (1 if pay_events else 0)
            revenue += sum(int((e.metadata_json or {}).get("amount_cents", 0)) for e in pay_events)
        rows.append(
            {
                "code": source.code,
                "kind": source.kind,
                "bot_starts": starts,
                "purchases": purchases,
                "revenue_cents": revenue,
            }
        )
    return rows


def cohorts(session: Session) -> list[dict]:
    """Monthly first-seen cohorts → converted users (PAYMENT_SUCCESS).
    Month grouping happens in Python: strftime() does not exist on PostgreSQL."""
    users = session.execute(select(User.id, User.created_at)).all()
    by_month: dict[str, list[int]] = {}
    for uid, created_at in users:
        by_month.setdefault(created_at.strftime("%Y-%m"), []).append(uid)

    out: list[dict] = []
    for cohort_month in sorted(by_month):
        ids = by_month[cohort_month]
        converted = 0
        for uid in ids:
            hit = session.execute(
                select(Event.id).where(Event.user_id == uid, Event.event_type == "PAYMENT_SUCCESS").limit(1)
            ).scalar_one_or_none()
            converted += hit is not None
        out.append(
            {
                "cohort": cohort_month,
                "users": len(ids),
                "converted": converted,
                "conversion": round(converted / len(ids), 4) if ids else 0.0,
            }
        )
    return out


def revenue_summary(session: Session) -> dict:
    """Total/renewal/referral/win-back revenue from event metadata (cents)."""

    def _sum(event_type: str, key: str = "amount_cents") -> int:
        rows = (
            session.execute(select(Event.metadata_json).where(Event.event_type == event_type)).scalars().all()
        )
        return sum(int((m or {}).get(key, 0)) for m in rows)

    return {
        "total_cents": _sum("PAYMENT_SUCCESS"),
        "renewal_cents": _sum("SERVICE_RENEWED"),
        "winback_cents": _sum("WINBACK_SUCCESS"),
    }


def referral_stats(session: Session) -> dict:
    total = session.execute(select(func.count()).select_from(Referral)).scalar_one()
    valid = session.execute(
        select(func.count()).select_from(Referral).where(Referral.status.in_(("valid", "rewarded")))
    ).scalar_one()
    rewarded = session.execute(
        select(func.count()).select_from(Referral).where(Referral.status == "rewarded")
    ).scalar_one()
    return {"total": total, "valid": valid, "rewarded": rewarded}


def campaign_performance(session: Session) -> list[dict]:
    rows = session.execute(
        select(Event.campaign_id, Event.event_type, func.count())
        .where(Event.campaign_id.isnot(None))
        .group_by(Event.campaign_id, Event.event_type)
    ).all()
    by_campaign: dict[int, dict] = {}
    for campaign_id, event_type, count in rows:
        entry = by_campaign.setdefault(campaign_id, {"campaign_id": campaign_id})
        entry[event_type] = count
    return list(by_campaign.values())


def lead_score(session_session, session: Session, user_id: int, weights: dict | None) -> int:
    """Configurable scoring: event-type weights summed over a user's events.
    Weights come from AppConfig('lead_scoring') — never hardcoded strategy."""
    w = weights or {}
    rows = session.execute(
        select(Event.event_type, func.count()).where(Event.user_id == user_id).group_by(Event.event_type)
    ).all()
    return sum(int(w.get(event_type, 0)) * count for event_type, count in rows)
