"""Immediate purchase guidance for existing, registered business-email links."""

import hashlib

from sqlalchemy import select

from pv_growth.acquisition import expansion_report, maps_report, needs_report, outreach_report
from pv_growth.acquisition.retail_catalogue import current_catalogue
from pv_growth.acquisition.retail_offer import body, current_offer, keyboard
from pv_growth.attribution.service import attribute, parse_start_param
from pv_growth.database.models import AppConfig, Source
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.mirza_adapter.retail_sales import aware

CAMPAIGNS = (
    outreach_report,
    maps_report,
    needs_report,
    expansion_report,
)


def registered_source(session, start_param, *, now):
    parsed = parse_start_param(start_param)
    if parsed.kind != "social":
        return None
    source = session.scalar(select(Source).where(Source.code == parsed.code, Source.kind == "social"))
    if source is None:
        return None
    for campaign in CAMPAIGNS:
        if parsed.code not in campaign.SOURCE_CODES:
            continue
        row = session.get(AppConfig, campaign.CONFIG_KEY)
        try:
            start, end = campaign.configuration(row.value if row else None, now)
            if end != aware(now) or not start <= aware(now):
                return None
            if not any(
                route["source_code"] == parsed.code and route["start_parameter"] == start_param
                for route in row.value["sources"]
            ):
                return None
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return source
    return None


def record_entry(session, user, *, start_param, request_id):
    now = utcnow()
    source = registered_source(session, start_param, now=now)
    if source is None:
        return False, None
    stamp = request_id or f"hour:{now:%Y%m%d%H}"
    digest = hashlib.sha256(f"{user.id}:{stamp}".encode()).hexdigest()
    _, created = ingest(
        session,
        "RETAIL_OFFER_REQUESTED",
        user_id=user.id,
        source_id=source.id,
        idempotency_key=f"retail:offer:{digest}",
        metadata={
            "source": "campaign",
            "source_code": source.code,
            "variant": "personal",
            "need": None,
            "version": "20261025",
            "observation": "user_request_not_price_view_or_checkout",
        },
    )
    if created:
        ingest(
            session,
            "BOT_STARTED",
            user_id=user.id,
            idempotency_key=f"botstart:{user.telegram_user_id}:{user.created_at:%Y%m%d%H}",
        )
        attribute(session, user.id, start_param, occurred_at=now)
    session.commit()
    result = (
        (
            body(
                "personal",
                offer=current_offer(session, now=now),
                catalogue=current_catalogue(session, now=now),
            ),
            keyboard(),
        )
        if created
        else None
    )
    return True, result
