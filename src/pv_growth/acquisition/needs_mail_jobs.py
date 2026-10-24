"""Docker-owned polling for the one authorized pilot. Absent secret file = off."""

import json
import os
import stat
import threading
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from sqlalchemy import text

from pv_growth.acquisition.mail_replies import NEEDS_END as END
from pv_growth.acquisition.mail_replies import process as shared_process
from pv_growth.acquisition.mail_replies import validate_config as shared_validate
from pv_growth.acquisition.mail_replies import verify_pilot as shared_verify
from pv_growth.acquisition.mail_transport import NeedsGmailTransport as GmailTransport
from pv_growth.acquisition.needs_report import CONFIG_KEY, configuration
from pv_growth.acquisition.post_report import _save
from pv_growth.acquisition.reply_ledger import NEEDS_CAMPAIGN
from pv_growth.acquisition.retail_catalogue import current_catalogue
from pv_growth.database.base import get_engine, session_scope
from pv_growth.database.models import AppConfig
from pv_growth.database.types import utcnow
from pv_growth.jobs.runner import handler
from pv_growth.mirza_adapter.retail_sales import _classification, aware

process = partial(shared_process, campaign=NEEDS_CAMPAIGN)
validate_config = partial(shared_validate, campaign=NEEDS_CAMPAIGN)
verify_pilot = partial(shared_verify, campaign=NEEDS_CAMPAIGN)
CONFIG_PATH = Path("/run/pv-growth-mail/needs261024.json")
PREFIX = "needs-mail-replies:20261024"
_LOCAL_LOCK = threading.Lock()
_LOCK_ID = 261024201


def bucket(now):
    now = aware(now)
    return now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0).strftime("%Y%m%d%H%M")


def load_config(settings, now):
    try:
        if CONFIG_PATH.is_symlink() or (settings.env == "production" and os.name != "posix"):
            raise ValueError
        fd = os.open(CONFIG_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError
            if settings.env == "production" and (info.st_mode & 0o077 or info.st_uid not in {0, os.getuid()}):
                raise ValueError
            data = file.read(65537)
        if len(data) > 65536:
            raise ValueError
        value = json.loads(data)
        routes = validate_config(value, now)
        if settings.env == "production":
            verify_pilot(routes)
        return value
    except (OSError, ValueError, TypeError):
        raise ValueError("private mail configuration unavailable") from None


@contextmanager
def campaign_lock(settings):
    engine = get_engine(settings)
    if engine.dialect.name != "postgresql":
        if settings.env == "production":
            raise ValueError("unexpected mail database")
        acquired = _LOCAL_LOCK.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                _LOCAL_LOCK.release()
        return
    with engine.connect() as connection:
        if (
            settings.env == "production"
            and connection.scalar(text("SELECT current_database()")) != "pv_growth"
        ):
            raise ValueError("unexpected mail database")
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_ID})
        try:
            yield bool(acquired)
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_ID})


def enqueue(session, now):
    from pv_growth.jobs.service import enqueue as enqueue_job

    if not CONFIG_PATH.is_file() or aware(now) >= END:
        return
    stamp = bucket(now)
    enqueue_job(
        session,
        "retail.needs_mail_replies",
        {"bucket": stamp},
        idempotency_key="retail-needs-mail:20261024:" + stamp,
        max_attempts=1,
    )


def _facts(settings, now):
    with session_scope(settings) as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        if settings.env == "production" and (
            db.bind.dialect.name != "postgresql"
            or db.scalar(text("SELECT current_database()")) != "pv_growth"
        ):
            raise ValueError("unexpected mail facts database")
        registry = db.get(AppConfig, CONFIG_KEY)
        start, end = configuration(registry.value if registry else None, now)
        if end != aware(now):
            raise ValueError("campaign unavailable")
        row = db.get(AppConfig, "retail-sales:classification")
        classification = row.value if row else None
        state, tests, _ = _classification(classification)
        catalogue = current_catalogue(db, now=now)
        if state != "owner_confirmed" or len(tests) != 3 or catalogue is None:
            raise ValueError("source facts unavailable")
        return classification, catalogue


@handler("retail.needs_mail_replies")
def monitor(session, settings, payload):
    now = utcnow()
    stamp = bucket(now)
    key = PREFIX + ":quarter:" + stamp
    if payload.get("bucket") != stamp or aware(now) >= END or session.get(AppConfig, key) is not None:
        return
    with campaign_lock(settings) as acquired:
        if not acquired:
            return
        try:
            private = load_config(settings, now)
            classification, catalogue = _facts(settings, now)
            routes = validate_config(private, now)
            recipients = {k: r["email"] for k, r in routes.items()}
            with GmailTransport(**private["credentials"], recipients=recipients) as api:
                report = process(
                    settings,
                    api,
                    config=private,
                    classification=classification,
                    catalogue=catalogue,
                    now=now,
                    lock_held=True,
                )
        except Exception as exc:
            # Provider text, private message content, addresses and credentials
            # must never reach job tracebacks, logs or the aggregate report.
            report = {
                "status": "unavailable",
                "data_complete": False,
                "error_class": type(exc).__name__,
                "campaign": "needs-led-acquisition-20261024",
            }
        report.update(
            observed_at_utc=aware(now).isoformat(),
            execution_owner="docker_scheduler",
            payment_confirmation="manual_support",
            sales_report_key="retail-needs:20261024:latest",
        )
        _save(session, key, report)
        _save(session, PREFIX + ":latest", report)
        session.commit()
