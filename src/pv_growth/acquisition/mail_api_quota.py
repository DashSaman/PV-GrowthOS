"""Shared request pacing; separate from daily mail and permanent send claims."""

import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig
from pv_growth.database.types import utcnow
from pv_growth.mirza_adapter.retail_sales import aware

KEY = "mail-api-rate:20261025"
RATE = 30  # 1,800 units/minute, below the documented 6,000-unit default.
_LOCK = threading.Lock()
_LOCK_ID = 2610251800
ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"


def units(method, url, params=None):
    if url == "https://oauth2.googleapis.com/token":
        return None
    if not url.startswith(ROOT + "/"):
        raise ValueError("unexpected Gmail quota endpoint")
    path = url[len(ROOT) :]
    fmt = (params or {}).get("format", "full")
    if method == "POST" and path == "/messages/send":
        return 100
    if method == "GET":
        if path == "/profile":
            return 1
        if path == "/threads":
            return 10
        if path.startswith("/threads/"):
            return 120 if fmt == "full" else 40
        if path == "/messages":
            return 5
        if path.startswith("/messages/"):
            return 20 if "/attachments/" in path else 60 if fmt in {"full", "raw", "html"} else 20
    raise ValueError("unsupported Gmail quota operation")


def reserve(settings, cost, now):
    if type(cost) is not int or not 1 <= cost <= 120:
        raise ValueError("invalid Gmail request cost")
    now = aware(now)
    with _LOCK, session_scope(settings) as db:
        if settings.env == "production" and (
            db.bind.dialect.name != "postgresql"
            or db.scalar(text("SELECT current_database()")) != "pv_growth"
        ):
            raise ValueError("unexpected Gmail quota database")
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_ID})
        row = db.get(AppConfig, KEY, with_for_update=True)
        slot = max(now, aware(datetime.fromisoformat(row.value["next_at_utc"]))) if row else now
        delay = (slot - now).total_seconds()
        if delay > 60:
            raise ValueError("Gmail request backlog exceeds bound")
        value = {"next_at_utc": (slot + timedelta(seconds=cost / RATE)).isoformat(), "units_per_second": RATE}
        if row:
            row.value = value
        else:
            db.add(AppConfig(key=KEY, value=value))
    return delay


def wait(settings, cost):
    delay = reserve(settings, cost, utcnow())
    if delay:
        time.sleep(delay)  # Transaction and advisory lock are already released.
