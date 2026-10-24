"""One shared rolling24h Gmail budget, with permanent non-replayable claims.

Known provider receipts are unioned with the fresh Sent snapshot, including
receipts not indexed yet. Unknown/crashed attempts consume an extra unit for24h.
Expiring a quota charge never releases an operation or reply phase. Initial
outreach is capped at450 total including the first20 already attempted.
"""

import hashlib
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select, text

from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig
from pv_growth.mirza_adapter.retail_sales import aware

KEY = "mail-shared-budget:20261024"
_LOCK = threading.Lock()
_LOCK_ID = 261024500


def _snapshot(values):
    if (
        not isinstance(values, (list, tuple))
        or len(values) > 500
        or any(not isinstance(v, str) or not re.fullmatch(r"[A-Fa-f0-9]{1,128}", v) for v in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("invalid complete Sent snapshot")
    return {hashlib.sha224(v.lower().encode()).hexdigest() for v in values}


def _fresh(observed_at, now):
    try:
        return 0 <= (aware(now) - aware(observed_at)).total_seconds() <= 30
    except (ValueError, TypeError, AttributeError):
        return False


@contextmanager
def _transaction(settings):
    with _LOCK:
        with session_scope(settings) as db:
            if settings.env == "production" and (
                db.bind.dialect.name != "postgresql"
                or db.scalar(text("SELECT current_database()")) != "pv_growth"
            ):
                raise ValueError("unexpected mail budget database")
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_ID})
            yield db


def initialize(settings, *, observed_sent, observed_at, initial_already_attempted, now):
    _snapshot(observed_sent)
    if (
        type(initial_already_attempted) is not int
        or not 0 <= initial_already_attempted <= 450
        or not _fresh(observed_at, now)
    ):
        raise ValueError("invalid account budget baseline")
    with _transaction(settings) as db:
        if db.get(AppConfig, KEY) is not None:
            raise ValueError("budget already initialized; no reset")
        db.add(
            AppConfig(
                key=KEY,
                value={
                    "started_at_utc": aware(now).isoformat(),
                    "initial_attempts": initial_already_attempted,
                    "attempts": 0,
                    "shared_limit": 500,
                    "initial_limit": 450,
                    "events": [],
                    "no_claim_release": True,
                },
            )
        )


def reserve(settings, operation, kind, *, observed_sent, observed_at, now):
    if (
        kind not in {"initial", "reply"}
        or not isinstance(operation, str)
        or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,200}", operation)
    ):
        raise ValueError("invalid budget operation")
    sent = _snapshot(observed_sent)
    if not _fresh(observed_at, now):
        return None
    opkey = "mail:q:" + hashlib.sha224(operation.encode()).hexdigest()
    with _transaction(settings) as db:
        row = db.scalar(select(AppConfig).where(AppConfig.key == KEY).with_for_update())
        if row is None or db.get(AppConfig, opkey) is not None:
            return None
        value = row.value
        if aware(now) < datetime.fromisoformat(value["started_at_utc"]):
            return None
        events = [
            v for v in value["events"] if datetime.fromisoformat(v["at"]) > aware(now) - timedelta(days=1)
        ]
        known = {v["receipt_hash"] for v in events if v.get("receipt_hash")}
        used = len(sent | known) + sum(not v.get("receipt_hash") for v in events)
        if used >= 500 or (kind == "initial" and value["initial_attempts"] >= 450):
            return None
        events.append({"key": opkey, "at": aware(now).isoformat(), "receipt_hash": None})
        row.value = {
            **value,
            "events": events,
            "initial_attempts": value["initial_attempts"] + (kind == "initial"),
            "attempts": value["attempts"] + 1,
            "last_charged_count": used + 1,
        }
        db.add(
            AppConfig(
                key=opkey,
                value={
                    "kind": kind,
                    "reserved_at_utc": aware(now).isoformat(),
                    "state": "attempt_consumed_no_refund",
                },
            )
        )
        db.commit()
    return opkey


def finish(settings, claim, gmail_id):
    if not isinstance(claim, str) or not re.fullmatch(r"mail:q:[0-9a-f]{56}", claim):
        raise ValueError("invalid budget claim")
    hashed = next(iter(_snapshot([gmail_id])))
    with _transaction(settings) as db:
        row = db.scalar(select(AppConfig).where(AppConfig.key == KEY).with_for_update())
        op = db.get(AppConfig, claim, with_for_update=True)
        if row is None or op is None or (op.value.get("receipt_hash") and op.value["receipt_hash"] != hashed):
            raise ValueError("missing or conflicting budget receipt")
        events = [dict(v) for v in row.value["events"]]
        for event in events:
            if event["key"] == claim:
                if event.get("receipt_hash") and event["receipt_hash"] != hashed:
                    raise ValueError("conflicting receipt")
                event["receipt_hash"] = hashed
        row.value = {**row.value, "events": events}
        op.value = {**op.value, "receipt_hash": hashed, "state": "provider_accepted_no_refund"}
