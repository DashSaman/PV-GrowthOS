"""Hourly aggregate reporting for the separately authorized expansion in frozen batches, up to430 contacts.

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

CONFIG_KEY = "retail-expand:campaign:20261024"
PREFIX = "retail-expand:20261024"
SOURCE_CODES = tuple(f"social:expand261024_e{i:03}" for i in range(1, 431))


def configuration(value, now):
    if not isinstance(value, dict):
        raise ValueError("campaign registry unavailable")
    if (
        value.get("stage") != "registered"
        or value.get("campaign") != "needs-expansion-20261024"
        or value.get("owner_authorized_limit") != 430
    ):
        raise ValueError("unexpected campaign registry")
    start = datetime.fromisoformat(value["registered_at_utc"])
    if start.tzinfo is None or aware(start) >= aware(now):
        raise ValueError("invalid campaign start")
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 430:
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
    if len(actual) != len(sources) or not actual <= expected:
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
    if not 1 <= len(codes) <= 430 or len(set(codes)) != len(codes) or not set(codes) <= set(SOURCE_CODES):
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
    first_paid = {}
    paid = list(_paid(snapshot))
    for uid, _role, _amount, stamp, _kind, _row in paid:
        first_paid[uid] = min(first_paid.get(uid, stamp), stamp)
    existing_buyers = set()
    for uid, role, amount, stamp, _, _row in paid:
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
        if first_paid[uid] < entered:
            existing_buyers.add(uid)
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
        "campaign": "needs-expansion-20261024",
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "sources": sources,
        "non_test_entered_accounts": len(anchors),
        "matched": {
            "service_buyers": len(set().union(*buyers.values())),
            "service_records": sum(counts.values()),
            "recorded_service_toman": format(sum(totals.values(), Decimal(0)), "f"),
        },
        "matched_existing_customer_buyers": len(existing_buyers),
        "matched_without_earlier_positive_history_buyers": len(
            set().union(*buyers.values()) - existing_buyers
        ),
        "verified_new_customer_count": None,
        "customer_newness_basis": "earlier_positive_history_proves_existing_absence_does_not_prove_new",
        "confirmed_internal_test_account_count": len(tests),
        "owner_50_percent_estimate_applied": False,
        "external_customer_verified": False,
        "causal_incremental_revenue_proven": False,
        "wallet_excluded": True,
        "amount_basis": "service_record_value_not_bank_settlement_or_profit",
        "entry_basis": "distinct_accounts_per_route_not_email_delivery_or_read",
        "attribution_rule": "first_campaign_source_same_account_positive_service_within_7_days",
    }


def review_deadline(registry):
    if registry.get("initial_send_closed") is not True:
        return None
    count = registry.get("accepted_initial_count")
    if type(count) is not int or not 1 <= count <= 430:
        return None
    stamp = datetime.fromisoformat(registry["last_accepted_at_utc"])
    start = datetime.fromisoformat(registry["registered_at_utc"])
    if (
        stamp.tzinfo is None
        or start.tzinfo is None
        or not aware(start) <= aware(stamp) < aware(start) + timedelta(days=31)
    ):
        raise ValueError("invalid initial acceptance time")
    return aware(stamp) + timedelta(hours=72)


def review72(report, registry, now, summarize_at):
    deadline = review_deadline(registry)
    if deadline is None or aware(now) < deadline:
        return None
    if report.get("status") != "ok" or report.get("data_complete") is not True:
        return None
    result = summarize_at(deadline)
    if result.get("status") != "ok" or result.get("data_complete") is not True:
        return None
    result.update(
        review_hours=72,
        review_deadline_utc=deadline.isoformat(),
        deadline_basis="72_hours_after_last_actual_gmail_acceptance",
        observed_at_utc=aware(now).isoformat(),
        accepted_initial_count=registry["accepted_initial_count"],
    )
    return result


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
        "retail.expansion_report",
        {"hour": hour},
        idempotency_key=f"retail-expand-report:20261024:{hour}",
        max_attempts=1,
    )


@handler("retail.expansion_report")
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
            registry_value = registry.value if registry else None
            start, end = configuration(registry_value, now)
            codes = tuple(s["source_code"] for s in registry_value["sources"])
            sources = list(db.scalars(select(Source).where(Source.code.in_(codes))))
            if len(sources) != len(codes) or any(
                s.kind != "social" or s.medium != "business_email" for s in sources
            ):
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
                        Source.code.in_(codes),
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
            source_codes=codes,
            classification=classification,
        )
        milestone = None
        if session.get(AppConfig, PREFIX + ":milestone:72") is None:
            milestone = review72(
                report,
                registry_value,
                now,
                lambda cutoff: summarize_outreach(
                    snapshot,
                    start=start,
                    end=cutoff,
                    entries=entries,
                    source_codes=codes,
                    classification=classification,
                ),
            )
        if milestone is not None:
            _save(session, PREFIX + ":milestone:72", milestone)
    except Exception as exc:  # isolated report; a failure is never zero sales
        report = {
            "status": "unavailable",
            "data_complete": False,
            "error_class": type(exc).__name__,
            "campaign": "needs-expansion-20261024",
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
    body = "📊 نتیجهٔ مسیرهای تماس تازه\n"
    if report.get("status") != "ok" or report.get("data_complete") is not True:
        return body + "دادهٔ ورود/خرید فعلاً ناموجود است؛ نتیجه صفر نیست."
    matched = report["matched"]
    body += (
        f"ورود یکتای غیرتست: {report['non_test_entered_accounts']}\n"
        f"خریدار مرتبط: {matched['service_buyers']} · {matched['recorded_service_toman']} تومان\n"
        "مسیر | ورود | خریدار | ارزش سرویس (تومان)\n"
    )
    for code in sorted(report["sources"]):
        source = report["sources"][code]
        body += (
            f"{code.rsplit('_', 1)[1]} | {source['entered_accounts']} | "
            f"{source['service_buyers']} | {source['recorded_service_toman']}\n"
        )
    body += (
        "e: هویت سایت کسب‌وکار؛ همهٔ سفارش‌های تست تأییدشده و کیف پول حذف‌اند.\n"
        "خرید به نخستین مسیر ثبت‌شدهٔ کمپین تا ۷ روز مرتبط است؛ "
        "مشتری بیرونی، علت انحصاری خرید یا سود تأیید نشده.\n"
        f"پنجره UTC: {report['start_utc']} تا {report['end_utc']}\n"
        f"مشاهده UTC: {report['observed_at_utc']}"
    )
    return body
