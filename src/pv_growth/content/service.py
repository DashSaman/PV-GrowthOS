"""Content engine: state machine + commercial-fact validation.

A content item may only move draft → validated when every commercial fact it
carries reconciles with the owning campaign record. Publishing is idempotent
via dedupe_key — scheduler double runs cannot double-post."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.content.publishers import Publisher
from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.database.models import Campaign, ContentItem
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.messaging import service as messaging

log = get_logger("content")

# fields that carry commercial meaning and must come from campaign data
COMMERCIAL_FACT_KEYS = {"price", "traffic_gb", "validity_hours", "discount_percent",
                        "max_claims", "locations", "uptime_percent", "user_count"}

_VALID_TRANSITIONS = {
    "draft": {"validated", "failed"},
    "validated": {"scheduled", "failed"},
    "scheduled": {"published", "failed"},
    "published": set(),
    "failed": {"draft"},
}


def validate_facts(session: Session, item: ContentItem) -> list[str]:
    """Return a list of problems (empty = valid). Commercial facts must match
    the campaign record when a campaign_code is present."""
    problems: list[str] = []
    facts = item.facts or {}
    if item.campaign_code:
        campaign = session.execute(
            select(Campaign).where(Campaign.code == item.campaign_code)
        ).scalar_one_or_none()
        if campaign is None:
            problems.append(f"campaign '{item.campaign_code}' not found")
        else:
            plan = dict(campaign.config or {})
            for key in COMMERCIAL_FACT_KEYS & set(facts):
                if key in plan and str(facts[key]) != str(plan[key]):
                    problems.append(f"fact '{key}'={facts[key]} contradicts campaign "
                                    f"({plan[key]})")
    # sanity: rating-like numbers must be positive
    for key in ("price", "traffic_gb", "validity_hours"):
        if key in facts:
            try:
                if float(facts[key]) <= 0:
                    problems.append(f"fact '{key}' must be positive")
            except (TypeError, ValueError):
                problems.append(f"fact '{key}' not numeric")
    # forbidden hype: fake scarcity/uptime/user counts without campaign backing
    if "uptime_percent" in facts and not item.campaign_code:
        problems.append("uptime claims require campaign data")
    if "user_count" in facts:
        problems.append("user_count claims are not allowed (no fake numbers)")
    return problems


def transition(session: Session, item: ContentItem, to_state: str) -> ContentItem:
    if to_state not in _VALID_TRANSITIONS.get(item.status, set()):
        raise ValidationError(f"illegal transition {item.status} → {to_state}")
    if to_state == "validated":
        problems = validate_facts(session, item)
        if problems:
            raise ValidationError("commercial facts invalid: " + "; ".join(problems))
    item.status = to_state
    item.updated_at = utcnow()
    session.flush()
    return item


def schedule(session: Session, item: ContentItem, when) -> ContentItem:
    if item.status != "validated":
        raise ValidationError("only validated content can be scheduled")
    item.scheduled_at = when
    return transition(session, item, "scheduled")


def publish_due(session: Session, settings: Settings, flags: FlagService,
                publishers: Mapping[str, Publisher], *, now=None) -> int:
    """Publish all due scheduled items (idempotent). Returns published count."""
    if not flags.enabled("CONTENT_ENGINE_ENABLED"):
        return 0
    now = now or utcnow()
    due = session.execute(
        select(ContentItem).where(
            ContentItem.status == "scheduled", ContentItem.scheduled_at <= now
        )
    ).scalars().all()
    published = 0
    for item in due:
        try:
            body = messaging.render(item.body, {**(item.facts or {}),
                                                "bot_link": "https://t.me/pvnetwork_bot"})
        except ValidationError as exc:
            item.status = "failed"
            log.warning("content render failed", item_id=item.id, error=str(exc))
            continue
        publisher = publishers.get(item.channel)
        if publisher is None:
            item.status = "failed"
            log.warning("content publisher unavailable", item_id=item.id, channel=item.channel)
            continue
        try:
            result = publisher.publish(item, body)
        except Exception as exc:  # noqa: BLE001 — publish failure is per-item
            if bool(getattr(exc, "retryable", False)):
                item.scheduled_at = now + timedelta(minutes=5)
                item.updated_at = utcnow()
                log.warning(
                    "content publish retry scheduled",
                    item_id=item.id,
                    retry_at=item.scheduled_at.isoformat(),
                    error=type(exc).__name__,
                )
            else:
                item.status = "failed"
                log.error("content publish failed", item_id=item.id, error=str(exc))
            continue
        item.status = "published"
        item.published_at = utcnow()
        item.message_id = int(result.remote_id) if result.remote_id.isdigit() else None
        ingest(session, "MESSAGE_SENT", user_id=None,
               idempotency_key=f"content:{item.id}",
               metadata={"kind": "channel_post", "content_id": item.id,
                         "channel": item.channel})
        published += 1
    if published:
        log.info("content published", count=published)
    return published
