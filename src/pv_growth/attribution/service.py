"""Attribution engine: first-touch (immutable) + last-touch (upserted).

Parses `start=<code>` deep-link params:
    freecfg_<campaign> | ref_<user> | partner_<partner> | seo_<page>
    | channel_<campaign> | social_<campaign> | <raw code>

Emits SOURCE_ATTRIBUTED events (idempotent per user+source so repeat clicks
on the same link do not spam the log).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.logging import get_logger
from pv_growth.database.models import AttributionTouch, Campaign, Event, Source
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest

log = get_logger("attribution")

_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("freecfg", re.compile(r"^freecfg[_-](?P<ref>.+)$")),
    ("ref", re.compile(r"^ref[_-](?P<ref>.+)$")),
    ("partner", re.compile(r"^partner[_-](?P<ref>.+)$")),
    ("seo", re.compile(r"^seo[_-](?P<ref>.+)$")),
    ("channel", re.compile(r"^channel[_-](?P<ref>.+)$")),
    ("social", re.compile(r"^social[_-](?P<ref>.+)$")),
]

VALID_START_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass
class ParsedStart:
    code: str          # canonical source code, e.g. "freecfg:summer"
    kind: str          # freecfg|ref|partner|seo|channel|social|organic|direct
    reference: str     # campaign/user/page/partner identifier ("" for organic)
    raw: str | None    # original start param


def parse_start_param(start_param: str | None) -> ParsedStart:
    raw = (start_param or "").strip()
    if not raw or raw.lower() in {"start", "menu"}:
        return ParsedStart(code="direct", kind="direct", reference="", raw=raw or None)
    if not VALID_START_RE.match(raw):
        return ParsedStart(code="direct", kind="direct", reference="", raw=raw)
    for kind, pattern in _KIND_PATTERNS:
        match = pattern.match(raw)
        if match:
            reference = match.group("ref")
            return ParsedStart(code=f"{kind}:{reference}", kind=kind, reference=reference, raw=raw)
    # bare custom code — treated as a channel-style source
    return ParsedStart(code=f"custom:{raw}", kind="channel", reference=raw, raw=raw)


def _resolve_source(session: Session, parsed: ParsedStart) -> Source:
    source = session.execute(select(Source).where(Source.code == parsed.code)).scalar_one_or_none()
    if source is not None:
        source.last_seen_at = utcnow()
        return source

    campaign_id: int | None = None
    if parsed.kind in {"freecfg", "channel", "social"} and parsed.reference:
        campaign = session.execute(
            select(Campaign).where(Campaign.code == parsed.reference)
        ).scalar_one_or_none()
        campaign_id = campaign.id if campaign else None

    source = Source(
        code=parsed.code,
        kind=parsed.kind,
        medium=parsed.kind,
        campaign_id=campaign_id,
    )
    session.add(source)
    session.flush()
    log.info("source created", code=parsed.code, kind=parsed.kind)
    return source


def attribute(
    session: Session,
    user_id: int,
    start_param: str | None,
    occurred_at: datetime | None = None,
) -> tuple[Source, bool]:
    """Record attribution for a bot start. Returns (source, first_touch_created).

    First-touch is only written when the user has none — never overwritten
    (unique constraint uq_touch_user_kind is the hard guarantee).
    """
    occurred_at = occurred_at or utcnow()
    parsed = parse_start_param(start_param)
    source = _resolve_source(session, parsed)
    source.bot_starts += 1

    first = session.execute(
        select(AttributionTouch).where(
            AttributionTouch.user_id == user_id, AttributionTouch.kind == "first"
        )
    ).scalar_one_or_none()
    first_created = first is None
    if first is None:
        session.add(AttributionTouch(
            user_id=user_id, kind="first", source_id=source.id,
            campaign_id=source.campaign_id, raw_start_param=parsed.raw,
            occurred_at=occurred_at,
        ))

    last = session.execute(
        select(AttributionTouch).where(
            AttributionTouch.user_id == user_id, AttributionTouch.kind == "last"
        )
    ).scalar_one_or_none()
    if last is None:
        session.add(AttributionTouch(
            user_id=user_id, kind="last", source_id=source.id,
            campaign_id=source.campaign_id, raw_start_param=parsed.raw,
            occurred_at=occurred_at,
        ))
    else:
        last.source_id = source.id
        last.campaign_id = source.campaign_id
        last.raw_start_param = parsed.raw
        last.occurred_at = occurred_at
        last.updated_at = utcnow()

    # idempotent SOURCE_ATTRIBUTED per user+source (repeat clicks don't spam)
    ingest(
        session, "SOURCE_ATTRIBUTED",
        user_id=user_id, source_id=source.id, campaign_id=source.campaign_id,
        occurred_at=occurred_at,
        idempotency_key=f"srcattr:{user_id}:{source.code}",
        metadata={"kind": parsed.kind, "reference": parsed.reference,
                  "raw": parsed.raw, "first_touch": first_created},
    )
    return source, first_created


def first_touch(session: Session, user_id: int) -> AttributionTouch | None:
    return session.execute(
        select(AttributionTouch).where(
            AttributionTouch.user_id == user_id, AttributionTouch.kind == "first"
        )
    ).scalar_one_or_none()


def user_source_history(session: Session, user_id: int) -> list[Event]:
    return list(session.execute(
        select(Event).where(Event.user_id == user_id, Event.event_type == "SOURCE_ATTRIBUTED")
        .order_by(Event.occurred_at)
    ).scalars())
