"""Bounded business-date reporting, independent of imported PAYMENT_SUCCESS events.

Only aggregates survive collection. A role is a current snapshot, not proof of
historical retail pricing. Wallet topups are never added to service order value.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from pv_growth.database.models import AppConfig, Event, MessageLog, User
from pv_growth.free_config.audience import _OTHER_PAID_TYPES, _TERMINAL, _time
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def summarize(snapshot, *, start, end, recipients=None, requests=None):
    start, end = aware(start), aware(end)
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError("invalid report window")
    roles = {str(r["id"]): str(r.get("agent", "")) for r in snapshot["users"]}
    quality = Counter()
    data = {k: [] for k in ("orders", "operations", "wallet")}
    for kind in data:
        for row in snapshot[kind]:
            status = str(row.get("Status", row.get("status", row.get("payment_Status", "")))).lower()
            if kind == "orders" and status not in {"active", *_TERMINAL}:
                continue
            if kind == "operations" and (
                row.get("type") not in _OTHER_PAID_TYPES or status not in {"paid", ""}
            ):
                continue
            if kind == "wallet" and status != "paid":
                continue
            try:
                amount = Decimal(
                    str(row.get("price_product" if kind == "orders" else "price")).replace(",", "")
                )
                if not amount.is_finite() or amount < 0:
                    raise ValueError
            except (ValueError, InvalidOperation):
                quality[kind + "_invalid_amount"] += 1
                continue
            if not amount:
                continue
            try:
                stamp = _time(row.get("time_sell" if kind == "orders" else "time"))
            except (ValueError, TypeError, OverflowError, OSError):
                quality[kind + "_invalid_time"] += 1
                continue
            uid = str(row.get("id_user"))
            role = roles.get(uid)
            segment = "ordinary" if role == "f" else "cooperation" if role in {"n", "n2"} else "unknown"
            data[kind].append((uid, segment, amount, stamp))

    def money(rows):
        return format(sum((r[2] for r in rows), Decimal(0)), "f")

    def window(a, b):
        result = {"start_utc": a.isoformat(), "end_utc": b.isoformat(), "segments": {}}
        for segment in ("ordinary", "cooperation", "unknown"):
            rows = {k: [r for r in vals if r[1] == segment and a <= r[3] < b] for k, vals in data.items()}
            services = rows["orders"] + rows["operations"]
            buyers = {r[0] for r in services}
            prior = {r[0] for k in ("orders", "operations") for r in data[k] if r[3] < a}
            result["segments"][segment] = {
                "orders": len(rows["orders"]),
                "operations": len(rows["operations"]),
                "service_buyers": len(buyers),
                "recorded_service_toman": money(services),
                "buyers_without_earlier_positive_record": len(buyers - prior),
                "wallet_toman_separate": money(rows["wallet"]),
            }
        return result

    def cohort(values):
        values = {str(uid): aware(stamp) for uid, stamp in (values or {}).items()}
        paid = [
            r
            for k in ("orders", "operations")
            for r in data[k]
            if r[0] in values and values[r[0]] <= r[3] < end
        ]
        return {
            "confirmed_users": len(values),
            "buyers_after_delivery": len({r[0] for r in paid}),
            "recorded_service_toman": money(paid),
        }

    return {
        "status": "ok",
        "data_complete": not bool(quality),
        "quality": dict(quality),
        "current": window(start, end),
        "previous": window(start - (end - start), start),
        "intro": cohort(recipients),
        "purchase_guidance": cohort(requests),
        "role_basis": "current_role_not_historical_price",
        "amount_basis": "service_recorded_not_cash_or_profit",
        "attribution": "temporal_observation_not_incremental_revenue",
    }


def collect_report(session, settings, *, key, end):
    """Cache one aggregate per fixed window; source failures are visible, never zero."""
    existing = session.get(AppConfig, key)
    if existing is not None:
        return existing.value
    end = aware(end)
    try:
        snapshot = MirzaMySQLReader(settings).fetch_retail_sales_snapshot()
        # Source measurements use a separate read-only PostgreSQL transaction.
        from pv_growth.database.base import session_scope

        with session_scope(settings) as db:
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
                if db.scalar(text("SELECT current_database()")) != "pv_growth":
                    raise ValueError("unexpected reporting database")
            recipients = dict(
                db.execute(
                    select(User.telegram_user_id, MessageLog.sent_at)
                    .join(MessageLog, MessageLog.user_id == User.id)
                    .where(
                        MessageLog.purpose == "pv_intro_5g",
                        MessageLog.status == "sent",
                        MessageLog.sent_at < end.replace(tzinfo=None),
                    )
                    .limit(5001)
                ).all()
            )
            events = db.execute(
                select(User.telegram_user_id, Event.occurred_at)
                .join(Event, Event.user_id == User.id)
                .where(
                    Event.event_type == "RETAIL_OFFER_REQUESTED", Event.occurred_at < end.replace(tzinfo=None)
                )
                .order_by(Event.occurred_at)
                .limit(20001)
            ).all()
            if len(recipients) > 5000 or len(events) > 20000:
                raise ValueError("report cohort row bound exceeded")
            requests = {}
            for uid, stamp in events:
                requests.setdefault(uid, stamp)
        report = summarize(
            snapshot, start=end - timedelta(days=1), end=end, recipients=recipients, requests=requests
        )
    except Exception:  # reporting failure must not suppress the ordinary admin report
        report = {"status": "unavailable", "end_utc": end.isoformat(), "reason": "bounded_source_unavailable"}
    try:
        with session.begin_nested():
            session.add(AppConfig(key=key, value=report))
            session.flush()
    except IntegrityError:
        existing = session.get(AppConfig, key)
        if existing is None:
            raise
        report = existing.value
    session.commit()
    return report


def render_report(report):
    if report.get("status") != "ok":
        return "📈 گزارش فروش: دادهٔ معتبر فعلاً ناموجود است؛ این وضعیت به معنی فروش صفر نیست."
    current, previous = report["current"], report["previous"]
    start, end = (
        datetime.fromisoformat(current[k]) + timedelta(hours=3, minutes=30) for k in ("start_utc", "end_utc")
    )
    body = f"📈 فروش ثبت‌شده · تومان\n🕒 تهران {start:%m/%d %H:%M} تا {end:%m/%d %H:%M}\n"
    for code, title in (("ordinary", "عادی"), ("cooperation", "همکاری"), ("unknown", "نقش نامشخص")):
        now, before = current["segments"][code], previous["segments"][code]
        body += (
            f"{title}: {now['service_buyers']} خریدار · {now['recorded_service_toman']} تومان\n"
            f"بازهٔ هم‌طول قبل: {before['service_buyers']} نفر · {before['recorded_service_toman']} تومان\n"
        )
    body += (
        f"🎁 خرید مشاهده‌شده پس از اشانتیون، تجمعی: {report['intro']['buyers_after_delivery']} نفر\n"
        f"🛒 کل درخواست‌کنندگان راهنمای خرید: {report['purchase_guidance']['confirmed_users']} نفر؛ "
        f"خرید بعدی {report['purchase_guidance']['buyers_after_delivery']} نفر\n"
        "نقش فعلی؛ سفارش/تمدید است، نه سود یا تسویه. شارژ کیف پول جداست. "
        "خرید بعد از پیام، اثر افزایشی کمپین را ثابت نمی‌کند؛ کلیک مستقیم خرید کامل ردیابی نمی‌شود."
    )
    if not report["data_complete"]:
        body += "\n⚠️ دادهٔ تاریخ/مبلغ ناقص است؛ جمع فقط ردیف‌های معتبر است."
    return body

