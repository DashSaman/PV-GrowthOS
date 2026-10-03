"""Event ingestion service — idempotent, append-only.

Every ingest goes through here (API, webhook, jobs, adapters). Duplicate
idempotency keys return the existing event with created=False; callers rely
on this instead of counting rows to decide whether side effects ran.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.core.logging import get_logger
from pv_growth.database.models import EVENT_TYPES, Event, User
from pv_growth.database.types import utcnow

log = get_logger("events")


def new_event_id() -> str:
    return uuid.uuid4().hex


def ingest(
    session: Session,
    event_type: str,
    *,
    user_id: int | None = None,
    campaign_id: int | None = None,
    source_id: int | None = None,
    occurred_at: datetime | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
    event_id: str | None = None,
) -> tuple[Event, bool]:
    """Persist one event. Returns (event, created). Raises ValidationError on
    unknown event type. Idempotent on idempotency_key."""
    if event_type not in EVENT_TYPES:
        raise ValidationError(f"unknown event type: {event_type}")

    if not idempotency_key:
        # no key -> no dedupe guarantee; deterministic hash would be wrong here,
        # so generate a unique one and let the caller learn to pass keys.
        idempotency_key = f"auto:{uuid.uuid4().hex}"

    existing = session.execute(
        select(Event).where(Event.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    if metadata is None:
        metadata = {}
    event = Event(
        event_id=event_id or new_event_id(),
        event_type=event_type,
        user_id=user_id,
        campaign_id=campaign_id,
        source_id=source_id,
        occurred_at=occurred_at or utcnow(),
        idempotency_key=idempotency_key,
        metadata_json=metadata,
    )
    try:
        # savepoint: a concurrent insert of the same key rolls back only this row
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        existing = session.execute(
            select(Event).where(Event.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False

    if user_id is not None:
        user = session.get(User, user_id)
        if user is not None:
            user.last_seen_at = utcnow()

    log.info("event ingested", event_type=event_type, user_id=user_id,
             campaign_id=campaign_id, source_id=source_id)
    return event, True


def get_or_create_user(
    session: Session,
    *,
    telegram_user_id: int | None = None,
    mirza_user_ref: str | None = None,
    username: str | None = None,
    first_name: str | None = None,
    language_code: str | None = None,
    telegram_chat_id: int | None = None,
) -> tuple[User, bool]:
    """Resolve a GrowthOS user by telegram id or mirza ref; create on first sight."""
    user: User | None = None
    if telegram_user_id is not None:
        user = session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        ).scalar_one_or_none()
    if user is None and mirza_user_ref:
        user = session.execute(
            select(User).where(User.mirza_user_ref == mirza_user_ref)
        ).scalar_one_or_none()
    if user is not None:
        user.last_seen_at = utcnow()
        if username:
            user.username = username
        if first_name:
            user.first_name = first_name
        if language_code:
            user.language_code = language_code
        if telegram_chat_id:
            user.telegram_chat_id = telegram_chat_id
        return user, False

    user = User(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        username=username,
        first_name=first_name,
        language_code=language_code,
        mirza_user_ref=mirza_user_ref,
    )
    session.add(user)
    session.flush()  # assign id
    log.info("user created", user_id=user.id, telegram_user_id=telegram_user_id)
    return user, True
