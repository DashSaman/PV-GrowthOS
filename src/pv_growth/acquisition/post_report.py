"""One public post -> requested bot entry -> same-account service purchases.

Mirza stays read-only. Matched means post-assisted within seven days, not causal
uplift or proof that an unclassified account belongs to an external customer.
"""

import hashlib
import json
import re
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, text

from pv_growth.acquisition.retail_offer import purchase_button
from pv_growth.database.models import AppConfig, Event, User
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.free_config.audience import _OTHER_PAID_TYPES, _TERMINAL, _amount, _time
from pv_growth.jobs.runner import handler
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
from pv_growth.mirza_adapter.retail_sales import _classification, aware, summarize
from pv_growth.telegram.client import InlineKeyboard

START_PARAMETER = "post_pv0_308"
POST = "pvnetwork0/308"
CONFIG_KEY = "retail-post-watch:pvnetwork0:308"
VIEW_URL = "https://t.me/pvnetwork0/308?embed=1&mode=tme"


def configuration(session, now):
    row = session.get(AppConfig, CONFIG_KEY)
    value = row.value if row else {}
    try:
        if value.get("status") != "active" or value.get("attribution_enabled") is not True:
            return None
        if value["post_message_id"] != 308 or value["post_url"] != "https://t.me/pvnetwork0/308":
            return None
        published = datetime.fromisoformat(value["published_at_utc"])
        tracking = datetime.fromisoformat(value["tracking_from_utc"])
        if published.tzinfo is None or tracking.tzinfo is None or not published <= tracking <= aware(now):
            return None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return {"published": aware(published), "tracking": aware(tracking)}


def record_entry(session, user, *, request_id):
    now = utcnow()
    active = configuration(session, now)
    if active:
        stamp = request_id or now.strftime("%Y%m%d%H")
        digest = hashlib.sha256(f"{user.id}:{stamp}".encode()).hexdigest()
        _, created = ingest(
            session,
            "RETAIL_POST_ENTERED",
            user_id=user.id,
            idempotency_key=f"retail-post-entry:{digest}",
            metadata={"post": POST, "basis": "explicit_post_link_start", "version": "20261018"},
        )
        session.commit()
        if not created:
            return None
    return (
        "🛒 خرید اشتراک شخصی PV Network\n\n"
        "برای دیدن پلن و فاکتور، دکمهٔ زیر را بزن.\n"
        "💳 پرداخت و تأیید وجه با پشتیبانی است؛ پس از شارژ کیف پول، خرید سرویس را تکمیل کن.\n"
        "ورود از لینک این پست برای بررسی نتیجهٔ تبلیغ ثبت می‌شود. "
        "قیمت و موجودی نهایی در ربات اصلی ملاک است.",
        InlineKeyboard([[purchase_button()]]),
    )


def _paid(snapshot):
    records = []
    roles = {str(row["id"]): row.get("agent") for row in snapshot["users"]}
    for kind in ("orders", "operations"):
        for row in snapshot[kind]:
            status = str(row.get("Status" if kind == "orders" else "status") or "").lower()
            if kind == "orders":
                if status not in {"active", *_TERMINAL}:
                    continue
            elif row.get("type") not in _OTHER_PAID_TYPES or status not in {"paid", ""}:
                continue
            amount = _amount(row.get("price_product" if kind == "orders" else "price"))
            if not amount:
                continue
            stamp = _time(row.get("time_sell" if kind == "orders" else "time"))
            uid = str(row["id_user"])
            records.append((uid, roles.get(uid), amount, stamp, kind, row))
    return records


def summarize_post(snapshot, *, start, end, entries, classification):
    start, end = aware(start), aware(end)
    state, tests, _ = _classification(classification)
    if state != "owner_confirmed":
        raise ValueError("test classification unavailable")
    whole = summarize(snapshot, start=start, end=end, classification=classification)
    if not whole["data_complete"]:
        raise ValueError("incomplete source history")
    records = _paid(snapshot)
    first_paid = {}
    for record in records:
        uid, stamp = record[0], record[3]
        first_paid[uid] = min(stamp, first_paid.get(uid, stamp))
    ordered_entries = {
        uid: sorted(aware(t) for t in values if start <= aware(t) < end) for uid, values in entries.items()
    }
    matched = []
    buyers = set()
    total = Decimal(0)
    for uid, role, amount, stamp, kind, row in records:
        account = hashlib.sha256(("mirza-user:" + uid).encode()).hexdigest()
        if role != "f" or account in tests or not start <= stamp < end:
            continue
        anchors = ordered_entries.get(uid, [])
        index = bisect_right(anchors, stamp) - 1
        if index < 0 or stamp - anchors[index] > timedelta(days=7):
            continue
        anchor = anchors[index]
        identity = str(row.get("id_invoice") or "")
        if kind == "orders" and not identity:
            raise ValueError("missing invoice identity")
        if kind == "operations":
            # Mirza has no operation PK: identical rows share a fingerprint,
            # remain separate records, and are not presented as unique invoices.
            identity = json.dumps([uid, row.get("type"), row.get("time"), str(amount)], separators=(",", ":"))
        key = hashlib.sha256((kind + ":" + identity).encode()).hexdigest()
        matched.append(
            {
                "purchase_key": key,
                "kind": kind,
                "business_at_utc": stamp.isoformat(),
                "recorded_service_toman": format(amount, "f"),
                "post_entry_at_utc": anchor.isoformat(),
                "had_earlier_positive_service_record": first_paid[uid] < anchor,
                "identity_basis": "invoice_fingerprint"
                if kind == "orders"
                else "operation_composite_not_unique",
            }
        )
        buyers.add(uid)
        total += amount
    remaining = whole["current"]["customer_quality"]
    occurrences = Counter(r["purchase_key"] for r in matched)
    return {
        "status": "ok",
        "data_complete": True,
        "post": POST,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "confirmed_internal_test_excluded": remaining["confirmed_internal_test"],
        "ordinary_after_test_exclusions": remaining["ordinary_remaining_unverified"],
        "matched": {
            "service_buyers": len(buyers),
            "service_records": len(matched),
            "recorded_service_toman": format(total, "f"),
        },
        "matched_orders": [
            {**r, "same_fingerprint_records": occurrences[r["purchase_key"]]} for r in matched[:500]
        ],
        "matched_order_list_truncated": len(matched) > 500,
        "attribution_rule": "same_account_explicit_post_entry_then_paid_service_within_7_days",
        "causal_incremental_revenue_proven": False,
        "external_customer_verified": False,
        "amount_basis": "service_record_value_not_bank_settlement_or_profit",
        "wallet_excluded": True,
        "owner_50_percent_scenario_applied": False,
    }


def read_views():
    try:
        with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as client:
            with client.stream("GET", VIEW_URL) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > 1024 * 1024:
                        raise ValueError("public post response bound")
                    content.extend(chunk)
        pattern = (
            r'data-post="pvnetwork0/308"(?:(?!data-post=)[\s\S]){0,30000}?'
            r'class="tgme_widget_message_views">([0-9.,KMBkmb]{1,20})</span>'
        )
        match = re.search(pattern, content.decode("utf8", errors="replace"))
        if not match:
            raise ValueError("post counter unavailable")
        return {
            "status": "ok",
            "displayed_views": match[1],
            "observed_at_utc": aware(utcnow()).isoformat(),
            "basis": "cumulative_display_counter_not_unique_people",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "error_class": type(exc).__name__}


def enqueue_monitor(session, now):
    from pv_growth.jobs.service import enqueue

    if configuration(session, now) is not None:
        hour = now.strftime("%Y%m%d%H")
        enqueue(
            session,
            "retail.post_report",
            {"hour": hour},
            idempotency_key=f"retail-post-report:{hour}",
            max_attempts=1,
        )


def _save(session, key, value):
    row = session.get(AppConfig, key)
    if row is None:
        session.add(AppConfig(key=key, value=value))
    else:
        row.value = value


@handler("retail.post_report")
def monitor(session, settings, payload):
    from pv_growth.database.base import session_scope

    now = utcnow()
    hour = now.strftime("%Y%m%d%H")
    key = f"retail-post:hour:{hour}"
    cfg = configuration(session, now)
    if payload.get("hour") != hour or cfg is None or session.get(AppConfig, key) is not None:
        return
    views = read_views()
    try:
        snapshot = MirzaMySQLReader(settings).fetch_retail_sales_snapshot()
        with session_scope(settings) as db:
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
                if (
                    settings.env == "production"
                    and db.scalar(text("SELECT current_database()")) != "pv_growth"
                ):
                    raise ValueError("unexpected reporting database")
            row = db.get(AppConfig, "retail-sales:classification")
            classification = row.value if row else None
            rows = list(
                db.execute(
                    select(User.telegram_user_id, Event.occurred_at, Event.metadata_json)
                    .join(Event, Event.user_id == User.id)
                    .where(
                        Event.event_type == "RETAIL_POST_ENTERED",
                        Event.occurred_at >= cfg["tracking"].replace(tzinfo=None),
                        Event.occurred_at < now,
                    )
                    .limit(20001)
                )
            )
            if len(rows) > 20000:
                raise ValueError("post entry row bound")
        entries = {}
        for uid, stamp, metadata in rows:
            if uid and metadata.get("post") == POST:
                entries.setdefault(str(uid), []).append(aware(stamp))
        report = summarize_post(
            snapshot,
            start=cfg["published"],
            end=min(aware(now), cfg["published"] + timedelta(days=31)),
            entries=entries,
            classification=classification,
        )
        state, test_keys, _ = _classification(classification)
        report["non_test_entered_accounts"] = sum(
            hashlib.sha256(("mirza-user:" + uid).encode()).hexdigest() not in test_keys for uid in entries
        )
        for hours in (24, 72, 168):
            milestone_key = f"retail-post:milestone:{hours}"
            deadline = cfg["published"] + timedelta(hours=hours)
            if aware(now) >= deadline and session.get(AppConfig, milestone_key) is None:
                fixed = summarize_post(
                    snapshot,
                    start=cfg["published"],
                    end=deadline,
                    entries=entries,
                    classification=classification,
                )
                fixed.update(
                    {
                        "milestone_hours": hours,
                        "measured_at_utc": aware(now).isoformat(),
                        "views": views,
                        "tracking_from_utc": cfg["tracking"].isoformat(),
                    }
                )
                _save(session, milestone_key, fixed)
    except Exception as exc:  # isolated reporting; a failure is never zero sales
        report = {"status": "unavailable", "error_class": type(exc).__name__, "post": POST}
    report.update(
        {
            "observed_at_utc": aware(now).isoformat(),
            "views": views,
            "tracking_from_utc": cfg["tracking"].isoformat(),
            "execution_owner": "docker_scheduler",
            "customer_messages": 0,
            "payment_confirmation": "manual_support",
        }
    )
    _save(session, key, report)
    _save(session, "retail-post:latest", report)
    session.commit()


def report_text(session):
    row = session.get(AppConfig, "retail-post:latest")
    if row is None:
        return ""
    report = row.value
    views = report.get("views", {}).get("displayed_views", "ناموجود")
    text = f"\n\n📊 نتیجهٔ پست https://t.me/{POST}\nبازدید نمایشی: {views}\n"
    if report.get("status") != "ok":
        return text + "خرید قابل‌تطبیق فعلاً ناموجود است؛ نتیجه صفر نیست."
    matched = report["matched"]
    text += (
        f"ورود ثبت‌شده پس از حذف تست: {report['non_test_entered_accounts']}\n"
        f"خرید مرتبط با ورود از لینک پست: {matched['service_buyers']} خریدار · "
        f"{matched['recorded_service_toman']} تومان\n"
        "همهٔ سفارش‌های تست تأییدشده حذف تحلیلی شده‌اند؛ کیف پول جداست.\n"
        "تطبیق حساب/خرید تا ۷ روز است؛ علت انحصاری خرید یا مشتری بیرونی تأییدشده نیست.\n"
        f"زمان مشاهده UTC: {report['observed_at_utc']}"
    )
    return text
