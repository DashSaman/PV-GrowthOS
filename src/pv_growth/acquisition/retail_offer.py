"""User-requested purchase guidance; no unsolicited messages or invented prices."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pv_growth.database.models import AppConfig, Event
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.telegram.client import InlineKeyboard

SOURCES = {"menu", "owned", "web", "share"}
NEEDS = {"daily", "work"}
PURCHASE_URL = "https://t.me/pvnetwork_bot?start=buy"


def purchase_button(text="🛒 انتخاب پلن و خرید سرویس شخصی"):
    # Mirza explicitly handles /start buy without treating it as a referral.
    return {"text": text, "url": PURCHASE_URL}


def current_offer(session, *, now=None):
    """An owner-verified catalogue entry expires; historical orders aren't prices."""
    row = session.get(AppConfig, "retail-offer:active")
    value = row.value if row else None
    if not isinstance(value, dict) or value.get("source") != "owner_confirmed":
        return None
    now = now or utcnow()
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    try:
        verified = datetime.fromisoformat(value["verified_at_utc"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(value["expires_at_utc"].replace("Z", "+00:00"))
        if verified.tzinfo is None or expires.tzinfo is None:
            return None
        if not verified <= now < expires or not timedelta(0) < expires - verified <= timedelta(hours=24):
            return None
        bounds = {"volume_gb": 10000, "duration_days": 365, "device_limit": 100, "price_toman": 10**9}
        if any(type(value.get(k)) is not int or not 1 <= value[k] <= cap for k, cap in bounds.items()):
            return None
        if any(
            not isinstance(value.get(k), str)
            or not 1 <= len(value[k].strip()) <= 160
            or any(c in value[k] for c in "<>\n\r")
            for k in ("name", "support")
        ):
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return {k: value[k] for k in (*bounds, "name", "support", "verified_at_utc", "expires_at_utc")}


def _digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def guide_button(source):
    if source not in SOURCES:
        raise ValueError("unknown guide source")
    return {"text": "💡 راهنمای انتخاب و خرید", "url": f"https://t.me/pvgrowthos_bot?start=buy_{source}"}


def body(variant, need=None, offer=None):
    opening = (
        "تست را روی اینترنت خودت پسندیدی؟ برای ادامه، اشتراک شخصی با حجم و مدت مستقل بگیر."
        if variant == "continuity"
        else "برای استفادهٔ مداوم، اشتراک شخصی بگیر؛ حجم و اعتبار آن با کاربران تست مشترک مصرف نمی‌شود."
    )
    tip = {
        "daily": "📱 برای استفادهٔ روزمره و اینستاگرام، حجم متناسب با مصرف و اینترنتت را انتخاب کن.",
        "work": "🖥 برای کار و چند دستگاه، محدودیت تعداد اتصال و حجم همان پلن را بررسی کن.",
    }.get(need, "📱 حجم، مدت و تعداد دستگاه موردنیازت را پیش از پرداخت انتخاب کن.")
    plan = (
        f"📦 {offer['name']} · {_digits(offer['volume_gb'])} گیگابایت · "
        f"{_digits(offer['duration_days'])} روز · {_digits(offer['device_limit'])} دستگاه\n"
        f"💳 قیمت تأییدشده: {offer['price_toman']:,} تومان\n"
        f"💬 {offer['support']}\n"
        "مبلغ نهایی و موجودی همان لحظه در فاکتور ربات اصلی ملاک پرداخت است.\n\n"
        if offer
        else "📦 حجم، مدت، تعداد دستگاه و قیمت جاری را در فهرست پلن‌های ربات اصلی ببین. "
        "قیمت تأییدشده‌ای برای نمایش در این راهنما موجود نیست.\n\n"
    )
    return (
        "🛒 سرویس اصلی PV Network · اشتراک شخصی تانل\n\n"
        f"{opening}\n\n{plan}{tip}\n\n"
        "۱️⃣ «انتخاب پلن و خرید» را بزن؛ وارد بخش خرید ربات رسمی می‌شوی. "
        "اگر دکمهٔ Start نمایش داده شد، آن را بزن.\n"
        "۲️⃣ سرویس و پلن موردنظرت را انتخاب کن؛ حجم، مدت، تعداد دستگاه و مبلغ فاکتور را بخوان.\n"
        "۳️⃣ فقط با روش‌های پرداخت نمایش‌داده‌شده در همان ربات پرداخت کن. "
        "اگر کیف پولت شارژ شد، به فاکتور برگرد و خرید سرویس را تکمیل کن.\n\n"
        "✅ سپس از «وضعیت خرید من» استفاده کن؛ شارژ کیف پول به‌تنهایی خرید سرویس نیست.\n"
        "💬 اگر تست روی اینترنت خودت وصل نشد، پیش از پرداخت راهنمای اتصال را ببین. "
        "برای اپل‌آیدی یا ترید، کشور و شرایط IP پلن را از پشتیبانی تأیید بگیر.\n"
        "🔐 رمز، کد ورود یا اطلاعات کارت را در این گفت‌وگو نفرست. "
        "خرید و فاکتور نهایی فقط در @pvnetwork_bot؛ راهنما شرط عضویت ندارد."
    )


def keyboard():
    return InlineKeyboard(
        [
            [{**purchase_button(), "style": "success"}],
            [
                {"text": "💳 راهنمای پرداخت", "callback_data": "retail:payment"},
                {"text": "✅ وضعیت خرید من", "callback_data": "retail:status"},
            ],
            [{"text": "🔧 راهنمای اتصال / اعلام مشکل", "callback_data": "retail:setup"}],
            [
                {"text": "📱 روزمره و اینستاگرام", "callback_data": "retail:daily"},
                {"text": "🖥 کار و چند دستگاه", "callback_data": "retail:work"},
            ],
        ]
    )


def record_request(session, user, *, source, request_id, need=None):
    if source not in SOURCES or (need is not None and need not in NEEDS):
        return None
    variant = "continuity" if user.id % 2 else "personal"
    stamp = request_id or f"hour:{utcnow():%Y%m%d%H}"
    digest = hashlib.sha256(f"{user.id}:{stamp}".encode()).hexdigest()
    _, created = ingest(
        session,
        "RETAIL_NEED_SELECTED" if need else "RETAIL_OFFER_REQUESTED",
        user_id=user.id,
        idempotency_key=f"retail:{'need' if need else 'offer'}:{digest}",
        metadata={
            "source": source,
            "variant": variant,
            "need": need,
            "version": "20261018",
            "observation": "user_request_not_price_view_or_checkout",
        },
    )
    session.commit()
    return (body(variant, need, current_offer(session)), keyboard()) if created else None


def selected_need(session, user, *, need, request_id):
    if need not in NEEDS:
        return None
    last = session.scalar(
        select(Event)
        .where(Event.user_id == user.id, Event.event_type == "RETAIL_OFFER_REQUESTED")
        .order_by(Event.occurred_at.desc(), Event.id.desc())
        .limit(1)
    )
    source = last.metadata_json.get("source", "menu") if last else "menu"
    return record_request(session, user, source=source, request_id=request_id, need=need)
