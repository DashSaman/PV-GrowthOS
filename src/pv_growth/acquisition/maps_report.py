"""Hourly aggregate reporting for the separately authorized ten Maps contacts.

This module cannot send customer messages. Purchases are first-campaign-source
assisted, not proof of incremental revenue or a verified external customer.
"""

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from pv_growth.acquisition.post_report import _paid, _save
from pv_growth.database.models import AppConfig, Event, Source, User
from pv_growth.database.types import utcnow
from pv_growth.jobs.runner import handler
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
from pv_growth.mirza_adapter.retail_sales import _classification, aware, summarize

CONFIG_KEY = "retail-maps:campaign:20261020"
PREFIX = "retail-maps:20261020"
SOURCE_CODES = tuple(f"social:maps261020_m{i:02}" for i in range(1, 11))


def configuration(value, now):
    if not isinstance(value, dict):
        raise ValueError("campaign registry unavailable")
    if (
        value.get("stage") != "registered"
        or value.get("campaign") != "maps-acquisition-20261020"
        or value.get("owner_authorized_limit") != 10
    ):
        raise ValueError("unexpected campaign registry")
    start = datetime.fromisoformat(value["registered_at_utc"])
    if start.tzinfo is None or aware(start) >= aware(now):
        raise ValueError("invalid campaign start")
    sources = value["sources"]
    if not isinstance(sources, list) or len(sources) != 10:
        raise ValueError("invalid campaign routes")
    expected = {
        (
            code,
            code.rsplit("_", 1)[1],
            code.replace("social:", "social_", 1),
            "business",
        )
        for code in SOURCE_CODES
    }
    actual = {(s["source_code"], s["key"], s["start_parameter"], s["segment"]) for s in sources}
    if actual != expected:
        raise ValueError("unexpected campaign route")
    start = aware(start)
    return start, min(aware(now), start + timedelta(days=31))


def summarize_outreach(snapshot, *, start, end, entries, source_codes, classification):
    start, end = aware(start), aware(end)
    state, tests, _ = _classification(classification)
    if state != "owner_confirmed" or len(tests) != 3:
        raise ValueError("test classification unavailable")
    whole = summarize(snapshot, start=start, end=end, classification=classification)
    if not whole["data_complete"]:
        raise ValueError("incomplete source history")
    codes = tuple(source_codes)
    if len(codes) != 10 or set(codes) != set(SOURCE_CODES):
        raise ValueError("unexpected campaign routes")
    visits = {code: set() for code in codes}
    anchors = {}
    for uid, code, stamp in entries:
        uid, stamp = str(uid), aware(stamp)
        if code not in visits or not start <= stamp < end:
            continue
        account = hashlib.sha256(("mirza-user:" + uid).encode()).hexdigest()
        if account in tests:
            continue
        visits[code].add(uid)
        anchor = (stamp, code)
        if uid not in anchors or anchor < anchors[uid]:
            anchors[uid] = anchor
    totals = {code: Decimal(0) for code in codes}
    buyers = {code: set() for code in codes}
    counts = dict.fromkeys(codes, 0)
    for uid, role, amount, stamp, _, _row in _paid(snapshot):
        anchor = anchors.get(uid)
        if role != "f" or anchor is None or not start <= stamp < end:
            continue
        entered, code = anchor
        if not entered <= stamp <= entered + timedelta(days=7):
            continue
        # Operations without a PK remain records, including identical rows.
        # No customer ID, invoice ID or individual receipt is exported.
        totals[code] += amount
        counts[code] += 1
        buyers[code].add(uid)
    sources = {
        code: {
            "entered_accounts": len(visits[code]),
            "service_buyers": len(buyers[code]),
            "service_records": counts[code],
            "recorded_service_toman": format(totals[code], "f"),
        }
        for code in codes
    }
    return {
        "status": "ok",
        "data_complete": True,
        "campaign": "maps-acquisition-20261020",
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "sources": sources,
        "non_test_entered_accounts": len(anchors),
        "matched": {
            "service_buyers": len(set().union(*buyers.values())),
            "service_records": sum(counts.values()),
            "recorded_service_toman": format(sum(totals.values(), Decimal(0)), "f"),
        },
        "confirmed_internal_test_account_count": len(tests),
        "owner_50_percent_estimate_applied": False,
        "external_customer_verified": False,
        "causal_incremental_revenue_proven": False,
        "wallet_excluded": True,
        "amount_basis": "service_record_value_not_bank_settlement_or_profit",
        "entry_basis": "distinct_accounts_per_route_not_email_delivery_or_read",
        "attribution_rule": "first_campaign_source_same_account_positive_service_within_7_days",
    }


def enqueue_monitor(session, now):
    from pv_growth.jobs.service import enqueue

    row = session.get(AppConfig, CONFIG_KEY)
    if row is None:
        return
    try:
        start, end = configuration(row.value, now)
        latest = session.get(AppConfig, PREFIX + ":latest")
        if (
            end == start + timedelta(days=31)
            and latest
            and latest.value.get("status") == "ok"
            and latest.value.get("data_complete") is True
            and latest.value.get("end_utc") == end.isoformat()
        ):
            return
    except (ValueError, TypeError, KeyError, AttributeError):
        # A damaged registered pilot produces an explicit unavailable report.
        # It must not interrupt the other scheduler jobs or imply zero sales.
        pass
    hour = now.strftime("%Y%m%d%H")
    enqueue(
        session,
        "retail.maps_report",
        {"hour": hour},
        idempotency_key=f"retail-maps-report:20261020:{hour}",
        max_attempts=1,
    )


@handler("retail.maps_report")
def monitor(session, settings, payload):
    from pv_growth.database.base import session_scope

    now = utcnow()
    hour = now.strftime("%Y%m%d%H")
    key = PREFIX + ":hour:" + hour
    if payload.get("hour") != hour or session.get(AppConfig, key) is not None:
        return
    if session.get(AppConfig, CONFIG_KEY) is None:
        return
    try:
        with session_scope(settings) as db:
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
                if (
                    settings.env == "production"
                    and db.scalar(text("SELECT current_database()")) != "pv_growth"
                ):
                    raise ValueError("unexpected reporting database")
            registry = db.get(AppConfig, CONFIG_KEY)
            start, end = configuration(registry.value if registry else None, now)
            sources = list(db.scalars(select(Source).where(Source.code.in_(SOURCE_CODES))))
            if len(sources) != 10 or any(s.kind != "social" or s.medium != "business_email" for s in sources):
                raise ValueError("campaign source rows unavailable")
            row = db.get(AppConfig, "retail-sales:classification")
            classification = row.value if row else None
            entries = list(
                db.execute(
                    select(User.telegram_user_id, Source.code, Event.occurred_at)
                    .select_from(Event)
                    .join(User, User.id == Event.user_id)
                    .join(Source, Source.id == Event.source_id)
                    .where(
                        Event.event_type == "SOURCE_ATTRIBUTED",
                        Source.code.in_(SOURCE_CODES),
                        Event.occurred_at >= start.replace(tzinfo=None),
                        Event.occurred_at < end.replace(tzinfo=None),
                    )
                    .limit(20001)
                )
            )
            if len(entries) > 20000 or any(not uid for uid, _code, _stamp in entries):
                raise ValueError("campaign entry row bound or identity unavailable")
        snapshot = MirzaMySQLReader(settings).fetch_retail_sales_snapshot()
        report = summarize_outreach(
            snapshot,
            start=start,
            end=end,
            entries=entries,
            source_codes=SOURCE_CODES,
            classification=classification,
        )
    except Exception as exc:  # isolated report; a failure is never zero sales
        report = {
            "status": "unavailable",
            "data_complete": False,
            "error_class": type(exc).__name__,
            "campaign": "maps-acquisition-20261020",
        }
    report.update(
        {
            "observed_at_utc": aware(now).isoformat(),
            "execution_owner": "docker_scheduler",
            "customer_messages": 0,
            "payment_confirmation": "manual_support",
        }
    )
    _save(session, key, report)
    _save(session, PREFIX + ":latest", report)
    session.commit()


def report_text(session):
    row = session.get(AppConfig, PREFIX + ":latest")
    if row is None:
        return ""
    report = row.value
    body = "📊 نتیجهٔ ۱۰ مسیر تماس خارجی\n"
    if report.get("status") != "ok" or report.get("data_complete") is not True:
        return body + "دادهٔ ورود/خرید فعلاً ناموجود است؛ نتیجه صفر نیست."
    matched = report["matched"]
    body += (
        f"ورود یکتای غیرتست: {report['non_test_entered_accounts']}\n"
        f"خریدار مرتبط: {matched['service_buyers']} · {matched['recorded_service_toman']} تومان\n"
        "مسیر | ورود | خریدار | ارزش سرویس (تومان)\n"
    )
    for code in SOURCE_CODES:
        source = report["sources"][code]
        body += (
            f"{code.rsplit('_', 1)[1]} | {source['entered_accounts']} | "
            f"{source['service_buyers']} | {source['recorded_service_toman']}\n"
        )
    body += (
        "m: کسب‌وکار تأییدشده؛ همهٔ سفارش‌های تست تأییدشده و کیف پول حذف‌اند.\n"
        "خرید به نخستین مسیر ثبت‌شدهٔ کمپین تا ۷ روز مرتبط است؛ "
        "مشتری بیرونی، علت انحصاری خرید یا سود تأیید نشده.\n"
        f"پنجره UTC: {report['start_utc']} تا {report['end_utc']}\n"
        f"مشاهده UTC: {report['observed_at_utc']}"
    )
    return body
