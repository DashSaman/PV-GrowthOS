"""Event ingestion API (service-to-service, admin-token protected)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from pv_growth.api.deps import rate_limit_events, require_admin
from pv_growth.core.config import get_settings
from pv_growth.core.errors import ValidationError
from pv_growth.database.base import session_scope
from pv_growth.database.models import Campaign, Event, Source

router = APIRouter(dependencies=[Depends(require_admin), Depends(rate_limit_events)])


class EventIn(BaseModel):
    event_type: str = Field(min_length=3, max_length=32)
    event_id: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=255)
    user_id: int | None = None
    telegram_user_id: int | None = None
    mirza_user_ref: str | None = Field(default=None, max_length=128)
    campaign_code: str | None = Field(default=None, max_length=64)
    source_code: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)


class EventOut(BaseModel):
    event_id: str
    event_type: str
    idempotency_key: str
    created: bool


@router.post("/events", response_model=EventOut, status_code=201)
def ingest_event(payload: EventIn) -> EventOut:
    from pv_growth.events.service import get_or_create_user, ingest

    settings = get_settings()
    with session_scope(settings) as session:
        user_id = payload.user_id
        if user_id is None and (payload.telegram_user_id or payload.mirza_user_ref):
            user, _ = get_or_create_user(
                session,
                telegram_user_id=payload.telegram_user_id,
                mirza_user_ref=payload.mirza_user_ref,
            )
            user_id = user.id

        campaign_id = None
        if payload.campaign_code:
            campaign = session.execute(
                select(Campaign).where(Campaign.code == payload.campaign_code)
            ).scalar_one_or_none()
            campaign_id = campaign.id if campaign else None

        source_id = None
        if payload.source_code:
            source = session.execute(
                select(Source).where(Source.code == payload.source_code)
            ).scalar_one_or_none()
            source_id = source.id if source else None

        try:
            event, created = ingest(
                session, payload.event_type,
                user_id=user_id, campaign_id=campaign_id, source_id=source_id,
                occurred_at=payload.occurred_at,
                idempotency_key=payload.idempotency_key,
                metadata=payload.metadata,
                event_id=payload.event_id,
            )
        except ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        # duplicate must not be reported as 201 (effects already ran on first)
        return EventOut(
            event_id=event.event_id,
            event_type=event.event_type,
            idempotency_key=event.idempotency_key,
            created=created,
        )


@router.get("/events")
def list_events(event_type: str | None = None, limit: int = 50) -> list[dict]:
    settings = get_settings()
    with session_scope(settings) as session:
        query = select(Event).order_by(Event.id.desc()).limit(min(limit, 200))
        if event_type:
            query = query.where(Event.event_type == event_type)
        rows = session.execute(query).scalars().all()
        return [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "user_id": e.user_id,
                "campaign_id": e.campaign_id,
                "source_id": e.source_id,
                "occurred_at": e.occurred_at.isoformat(),
            }
            for e in rows
        ]
