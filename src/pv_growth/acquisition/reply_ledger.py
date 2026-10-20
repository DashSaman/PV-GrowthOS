"""Durable at-most-one reply attempt, including after an ambiguous send.

Only derived fingerprints and the original pilot route/phase are stored.
No lease, reset, expiry or retry exists. A caller must hold the campaign's
processing lock and recheck opt-out before invoking its fixed mail transport.
"""

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig
from pv_growth.database.types import utcnow

INTENTS = frozenset({"interest", "price", "payment", "payment_reported", "setup", "collaboration"})
ROUTES = frozenset([f"a{i:02}" for i in range(1, 11)] + [f"s{i:02}" for i in range(1, 21)])
CAMPAIGN = "external-acquisition-20261019"
MAPS_CAMPAIGN = "maps-acquisition-20261020"
MAPS_ROUTES = frozenset(f"m{i:02}" for i in range(1, 11))


def routes_for(campaign):
    if campaign == CAMPAIGN:
        return ROUTES
    if campaign == MAPS_CAMPAIGN:
        return MAPS_ROUTES
    raise ValueError("unsupported mail campaign")


def source_url(campaign, route):
    _route(route, campaign)
    prefix = "social_maps261020_" if campaign == MAPS_CAMPAIGN else "social_ext261019_"
    return "https://t.me/pvgrowthos_bot?start=" + prefix + route


@dataclass(frozen=True)
class ReplyClaim:
    message_key: str
    phase_key: str


def _key(kind, value, *, campaign=CAMPAIGN):
    # AppConfig.key has a 64-character limit. SHA-224 gives a 63-char key.
    routes_for(campaign)
    fingerprint = hashlib.sha224((campaign + ":" + value).encode()).hexdigest()
    return "mail:" + kind + ":" + fingerprint


def _guard(db, settings):
    if settings.env == "production":
        if (
            db.bind.dialect.name != "postgresql"
            or db.scalar(text("SELECT current_database()")) != "pv_growth"
        ):
            raise ValueError("unexpected mail ledger database")


def _route(route, campaign=CAMPAIGN):
    if route not in routes_for(campaign):
        raise ValueError("route outside original pilot")


def claim_reply(
    settings, route: str, inbound_id: str, intent: str, *, campaign=CAMPAIGN
) -> ReplyClaim | None:
    """Commit both inbound and phase claims before allowing one send attempt."""
    _route(route, campaign)
    if intent not in INTENTS or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", inbound_id):
        raise ValueError("unsupported reply claim")
    claim = ReplyClaim(
        _key("m", inbound_id, campaign=campaign), _key("p", route + ":" + intent, campaign=campaign)
    )
    try:
        with session_scope(settings) as db:
            _guard(db, settings)
            if db.get(AppConfig, _key("s", route, campaign=campaign)) is not None:
                return None
            value = {
                "campaign": campaign,
                "route": route,
                "intent": intent,
                "state": "reserved",
                "reserved_at_utc": utcnow().isoformat(),
            }
            db.add(AppConfig(key=claim.message_key, value=dict(value)))
            db.add(AppConfig(key=claim.phase_key, value=dict(value)))
            db.commit()
        return claim
    except IntegrityError:
        # Atomic rollback leaves no partial claim. Neither an already seen
        # inbound nor previously attempted phase can result in another send.
        return None


def finish_reply(settings, claim: ReplyClaim, result: str) -> None:
    """Record outcome without ever releasing the committed send reservation."""
    if result not in {"sent", "unknown", "held"}:
        raise ValueError("unsupported send outcome")
    with session_scope(settings) as db:
        _guard(db, settings)
        rows = [db.get(AppConfig, key, with_for_update=True) for key in (claim.message_key, claim.phase_key)]
        if any(row is None or row.value.get("state") != "reserved" for row in rows):
            raise ValueError("missing or already finalized claim")
        for row in rows:
            row.value = {**row.value, "state": result, "finished_at_utc": utcnow().isoformat()}


def stop_route(settings, route: str, *, campaign=CAMPAIGN) -> None:
    """Permanently hold this route after a verified human opt-out."""
    _route(route, campaign)
    try:
        with session_scope(settings) as db:
            _guard(db, settings)
            key = _key("s", route, campaign=campaign)
            if db.get(AppConfig, key) is None:
                db.add(AppConfig(key=key, value={"route": route, "state": "stopped"}))
                db.commit()
    except IntegrityError:
        # Another processor already recorded the same stop; it stays stopped.
        return
