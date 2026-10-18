"""Private, requested checkout assistance. Mirza is read-only and owns payment.

No invoice creation, wallet movement, credential collection or marketing dispatch.
An observed service purchase after guidance is temporal evidence, not uplift.
"""

import hashlib
from datetime import UTC, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from pv_growth.acquisition.retail_offer import current_offer, keyboard, purchase_button
from pv_growth.database.models import AppConfig, Event
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.free_config.audience import _OTHER_PAID_TYPES, _POTENTIALLY_ACTIVE, _TERMINAL, _amount, _time
from pv_growth.jobs.runner import handler
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
from pv_growth.telegram.client import InlineKeyboard


def evaluate_status(snapshot, *, since, now):
    unavailable = {"status": "unavailable", "service_toman": "0"}
    user = snapshot.get("user")
    if user is None:
        return {"status": "not_registered", "service_toman": "0"}
    if user.get("agent") != "f" or user.get("User_Status") != "Active":
        return {"status": "outside_retail_scope", "service_toman": "0"}
    since = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    if not now - timedelta(days=7) <= since <= now:
        return unavailable
    total = Decimal(0)
    unpaid = False
    try:
        for kind in ("orders", "operations"):
            for row in snapshot[kind]:
                status = str(row.get("Status" if kind == "orders" else "status") or "").lower()
                if kind == "orders":
                    if status == "unpaid":
                        unpaid = True  # existence only: not a fresh checkout timestamp
                        continue
                    if status == "unsuccessful":
                        continue
                    if status not in _POTENTIALLY_ACTIVE | _TERMINAL:
                        return unavailable
                else:
                    operation = row.get("type")
                    if operation in {"gift_time", "gift_volume", "transfertouser"}:
                        continue
                    if operation not in _OTHER_PAID_TYPES:
                        return unavailable
                    if status == "unpaid":
                        unpaid = True
                        continue
                    if status not in {"paid", ""}:
                        return unavailable
                amount = _amount(row.get("price_product" if kind == "orders" else "price"))
                if not amount:
                    continue
                stamp = _time(row.get("time_sell" if kind == "orders" else "time"))
                if stamp > now:
                    return unavailable
                if since <= stamp <= now:
                    total += amount
    except (ValueError, TypeError, ArithmeticError, OverflowError, OSError, KeyError):
        return unavailable
    return {
        "status": "service_recorded" if total else "unpaid_invoice_present" if unpaid else "not_observed",
        "service_toman": format(total, "f"),
    }


def _status(session, settings, user):
    from pv_growth.database.base import session_scope

    with session_scope(settings) as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
            if settings.env == "production" and db.scalar(text("SELECT current_database()")) != "pv_growth":
                raise ValueError("unexpected retail reporting database")
        anchor = db.scalar(
            select(Event)
            .where(
                Event.user_id == user.id,
                Event.event_type == "RETAIL_OFFER_REQUESTED",
                Event.occurred_at >= utcnow() - timedelta(days=7),
                Event.occurred_at <= utcnow(),
            )
            .order_by(Event.occurred_at, Event.id)
            .limit(1)
        )
    if anchor is None or anchor.occurred_at < utcnow() - timedelta(days=7):
        return "برای بررسی خرید این مسیر، ابتدا /buy را باز کن و سپس خرید را در ربات اصلی انجام بده."
    try:
        result = evaluate_status(
            MirzaMySQLReader(settings).fetch_customer_checkout(user.telegram_user_id),
            since=anchor.occurred_at,
            now=utcnow(),
        )
    except Exception:
        result = {"status": "unavailable"}
    state = result["status"]
    if state == "service_recorded":
        return (
            f"✅ پس از درخواست راهنما، سرویس یا تمدید مثبت به مبلغ {result['service_toman']} تومان "
            "در ربات اصلی ثبت شده است.\nاین مشاهدهٔ سفارش سرویس است، نه رسید تسویهٔ بانکی. "
            "جزئیات و اطلاعات اتصال را از «سرویس‌های من» در @pvnetwork_bot بگیر.\n"
            "اگر روی اینترنت خودت وصل نشد، از راهنمای اتصال و پشتیبانی همان ربات کمک بگیر."
        )
    if state == "unpaid_invoice_present":
        return (
            "💳 فاکتور پرداخت‌نشده‌ای در ربات اصلی وجود دارد؛ هنوز خرید سرویس مثبت بعد از این راهنما "
            "مشاهده نشد. این فاکتور ممکن است قدیمی باشد.\n"
            "در @pvnetwork_bot فاکتور درست را باز کن و وضعیتش را بررسی کن. "
            "شارژ کیف پول به‌تنهایی خرید نیست؛ پس از شارژ، خرید سرویس را تکمیل کن. "
            "اگر پرداختت نتیجهٔ نامشخص دارد دوباره پرداخت نکن؛ رسید را فقط برای پشتیبانی رسمی بفرست."
        )
    if state in {"unavailable", "outside_retail_scope"}:
        return (
            "⚠️ وضعیت معتبر خرید مشتری عادی فعلاً ناموجود است؛ این به معنی پرداخت‌نکردن نیست.\n"
            "وضعیت فاکتور را در @pvnetwork_bot یا با پشتیبانی همان ربات بررسی کن. "
            "برای رفع ابهام دوباره پرداخت نکن."
        )
    return (
        "🛒 هنوز خرید سرویس مثبت پس از این راهنما در دادهٔ ربات اصلی مشاهده نشد؛ "
        "این نتیجه اثبات شکست پرداخت نیست.\n"
        "دکمهٔ خرید را بزن، پلن را انتخاب و فاکتور را بررسی کن. اگر پرداخت انجام داده‌ای، "
        "وضعیت را در ربات اصلی بررسی کن و در ابهام دوباره پرداخت نکن."
    )


def action(session, settings, user, *, code, request_id):
    if code not in {"payment", "status", "setup", "connected", "not_connected"}:
        return None
    digest = hashlib.sha256(f"{user.id}:{request_id}".encode()).hexdigest()
    _, created = ingest(
        session,
        "RETAIL_CONNECTION_REPORTED"
        if code in {"connected", "not_connected"}
        else "RETAIL_CHECK_REQUESTED"
        if code == "status"
        else "RETAIL_HELP_REQUESTED",
        user_id=user.id,
        idempotency_key=f"retail-action:{digest}",
        metadata={"action": code, "version": "20261018", "basis": "user_request_or_self_report"},
    )
    session.commit()  # reserve once before an external read; duplicate updates don't replay
    if not created:
        return None
    if code == "status":
        return _status(session, settings, user), keyboard()
    if code == "payment":
        return (
            "💳 پرداخت روشن، مرحله‌به‌مرحله\n\n"
            "۱. «انتخاب پلن و خرید» را بزن؛ پلن و مبلغ فاکتور را در ربات اصلی ببین.\n"
            "۲. فقط روش پرداخت نمایش‌داده‌شده در همان ربات را استفاده کن. "
            "اگر لازم بود کیف پول را شارژ کنی، بعد از شارژ به خرید سرویس برگرد.\n"
            "۳. پس از ثبت خرید، سرویس باید در بخش «سرویس‌های من» دیده شود.\n\n"
            "⚠️ اگر وجه کم شد و نتیجه مشخص نیست، دوباره پرداخت نکن؛ با پشتیبانی ربات اصلی "
            "وضعیت را بررسی کن. رمز، کد ورود و اطلاعات کارت را اینجا نفرست.",
            keyboard(),
        )
    if code == "connected":
        return (
            "✅ گزارش اتصال روی اینترنت خودت ثبت شد. برای ادامهٔ استفاده، شرایط پلن شخصی را ببین.",
            keyboard(),
        )
    body = (
        "🔧 راهنمای اتصال\n\n"
        "۱. اطلاعات اتصال را فقط از تحویل مربوط به خودت یا پست مجاز کانال کپی کن.\n"
        "۲. در برنامهٔ سازگار با نوع کانفیگ، گزینهٔ افزودن یا Import from clipboard را بزن.\n"
        "۳. اتصال را روشن کن و روی اینترنت خودت یک سایت و برنامهٔ موردنیازت را بررسی کن.\n"
        "۴. در صورت خطا، نام برنامه، نوع اینترنت و متن خطای بدون اطلاعات حساب را برای "
        "پشتیبانی @pvnetwork_bot بفرست؛ کانفیگ و رمز را در گروه عمومی نفرست.\n\n"
        "تأیید اتصال از سرور ما تضمین عملکرد اینترنت تو نیست. "
        "نتیجهٔ واقعی روی دستگاهت را با دکمه‌های زیر اعلام کن."
    )
    if code == "not_connected":
        body = "⚠️ مشکل اتصال ثبت شد؛ پیش از پرداخت از پشتیبانی کمک بگیر.\n\n" + body
    return body, InlineKeyboard(
        [
            [
                {"text": "✅ روی اینترنت من وصل شد", "callback_data": "retail:connected"},
                {"text": "❌ وصل نشد", "callback_data": "retail:not_connected"},
            ],
            [{"text": "💬 پشتیبانی در ربات اصلی", "url": "https://t.me/pvnetwork_bot"}],
            [purchase_button()],
        ]
    )


def enqueue_monitor(session, now):
    from pv_growth.jobs.service import enqueue

    bucket = now.strftime("%Y%m%d%H")
    enqueue(
        session,
        "retail.monitor",
        {"hour": bucket},
        idempotency_key=f"retail-monitor:{bucket}",
        max_attempts=1,
    )


@handler("retail.monitor")
def monitor(session, settings, payload):
    from pv_growth.database.base import session_scope
    from pv_growth.mirza_adapter.retail_sales import collect_report

    now = utcnow()
    hour = now.strftime("%Y%m%d%H")
    if payload.get("hour") != hour:
        return  # don't run stale hourly snapshots under today's timestamp
    key = f"retail-monitor:hour:{hour}"
    if session.get(AppConfig, key) is not None:
        return
    report = collect_report(
        session, settings, key=f"retail-sales:hour:{hour}", end=now.replace(minute=0, second=0, microsecond=0)
    )
    with session_scope(settings) as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
            if settings.env == "production" and db.scalar(text("SELECT current_database()")) != "pv_growth":
                raise ValueError("unexpected retail reporting database")
        offer = current_offer(db, now=now)
    session.add(
        AppConfig(
            key=key,
            value={
                "status": report["status"],
                "end_utc": report.get("current", {}).get("end_utc", report.get("end_utc")),
                "offer_state": "verified" if offer else "unavailable",
                "ordinary": report.get("current", {}).get("segments", {}).get("ordinary"),
                "previous_ordinary": report.get("previous", {}).get("segments", {}).get("ordinary"),
                "data_complete": report.get("data_complete", False),
                "attribution": "temporal_observation_not_incremental_revenue",
                "customer_messages": 0,
            },
        )
    )
    session.commit()
