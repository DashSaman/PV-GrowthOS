"""Lifecycle trigger evaluation.

Each trigger names a state; a user "matches" when the state holds and the
follow-up has not already converted. Rules then schedule a send with delay,
and the send worker enforces stop-conditions/cooldown/max-sends/idempotency.
"""

from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from pv_growth.database.models import Event, User

# trigger key -> (gate event, missing event) semantics
TRIGGERS = {
    "started_no_trial": ("BOT_STARTED", "TRIAL_CREATED"),
    "trial_no_connect": ("TRIAL_CREATED", "TRIAL_CONNECTED"),
    "trial_no_purchase": ("TRIAL_CONNECTED", "PAYMENT_SUCCESS"),
    "checkout_abandoned": ("CHECKOUT_STARTED", "PAYMENT_SUCCESS"),
}


def _has(session: Session, user_id: int, event_type: str) -> bool:
    return session.execute(
        select(exists().where(Event.user_id == user_id, Event.event_type == event_type))
    ).scalar_one()


def _latest_event_at(session, user_id: int, event_type: str):
    return session.execute(
        select(func.max(Event.occurred_at)).where(Event.user_id == user_id, Event.event_type == event_type)
    ).scalar_one()


def matches_trigger(session: Session, user: User, trigger: str) -> bool:
    if trigger in TRIGGERS:
        gate, missing = TRIGGERS[trigger]
        return _has(session, user.id, gate) and not _has(session, user.id, missing)
    if trigger == "first_purchase_onboarding":
        return _has(session, user.id, "PAYMENT_SUCCESS")
    if trigger == "winback":
        # expired trial + inactive 7+ days + no purchase
        if _has(session, user.id, "PAYMENT_SUCCESS"):
            return False
        started = _has(session, user.id, "BOT_STARTED")
        from datetime import timedelta

        from pv_growth.database.types import utcnow

        return started and user.last_seen_at is not None and utcnow() - user.last_seen_at >= timedelta(days=7)
    return False


def gate_event_for(trigger: str) -> str | None:
    if trigger in TRIGGERS:
        return TRIGGERS[trigger][0]
    if trigger == "first_purchase_onboarding":
        return "PAYMENT_SUCCESS"
    return "BOT_STARTED"


def candidate_users(
    session: Session,
    trigger: str,
    *,
    limit: int | None = None,
) -> list[tuple[User, object]]:
    """Return matching users with a set-based gate query, optionally bounded."""
    if trigger in TRIGGERS:
        gate, missing = TRIGGERS[trigger]
    elif trigger == "first_purchase_onboarding":
        gate, missing = "PAYMENT_SUCCESS", None
    elif trigger == "winback":
        gate, missing = "BOT_STARTED", "PAYMENT_SUCCESS"
    else:
        return []

    gate_rows = (
        select(
            Event.user_id.label("user_id"),
            func.max(Event.occurred_at).label("gate_at"),
        )
        .where(Event.event_type == gate, Event.user_id.isnot(None))
        .group_by(Event.user_id)
        .subquery()
    )
    statement = (
        select(User, gate_rows.c.gate_at)
        .join(gate_rows, gate_rows.c.user_id == User.id)
        .where(User.is_blocked.is_(False))
        .order_by(User.id)
    )
    if missing:
        statement = statement.where(
            ~exists().where(
                Event.user_id == User.id,
                Event.event_type == missing,
            )
        )
    if trigger == "winback":
        from datetime import timedelta

        from pv_growth.database.types import utcnow

        statement = statement.where(
            User.last_seen_at.isnot(None),
            User.last_seen_at <= utcnow() - timedelta(days=7),
        )
    if limit is not None:
        statement = statement.limit(max(0, limit))
    return [(user, gate_at) for user, gate_at in session.execute(statement).all()]
