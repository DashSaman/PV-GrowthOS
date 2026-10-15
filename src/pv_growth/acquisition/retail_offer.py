"""User-requested purchase guidance; no unsolicited messages or invented prices."""

import hashlib

from sqlalchemy import select

from pv_growth.database.models import Event
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.telegram.client import InlineKeyboard

SOURCES = {"menu", "owned", "web", "share"}
NEEDS = {"daily", "work"}
PURCHASE_URL = "https://t.me/pvnetwork_bot"


def guide_button(source):
    if source not in SOURCES:
        raise ValueError("unknown guide source")
    return {"text": "💡 راهنمای انتخاب و خرید", "url": f"https://t.me/pvgrowthos_bot?start=buy_{source}"}


def body(variant, need=None):
    opening = (
        "تست را روی اینترنت خودت پسندیدی؟ برای ادامه، اشتراک شخصی با حجم و مدت مستقل بگیر."
        if variant == "continuity"
        else "برای استفادهٔ مداوم، اشتراک شخصی بگیر؛ حجم و اعتبار آن با کاربران تست مشترک مصرف نمی‌شود."
    )
    tip = {
        "daily": "📱 برای استفادهٔ روزمره و اینستاگرام، حجم متناسب با مصرف و اینترنتت را انتخاب کن.",
        "work": "🖥 برای کار و چند دستگاه، محدودیت تعداد اتصال و حجم همان پلن را بررسی کن.",
    }.get(need, "📱 حجم، مدت و تعداد دستگاه موردنیازت را پیش از پرداخت انتخاب کن.")
    return (
        "🛒 سرویس اصلی PV Network · اشتراک شخصی تانل\n\n"
        f"{opening}\n\n{tip}\n"
        "۱️⃣ دکمهٔ خرید را بزن و وارد ربات رسمی شو.\n"
        "۲️⃣ بخش خرید سرویس را باز کن؛ شرایط و قیمت نهایی پلن را همان‌جا ببین.\n"
        "۳️⃣ قبل از پرداخت، حجم، مدت، کشور و راهنمای اتصال را بررسی کن.\n\n"
        "📍 برای اپل‌آیدی یا ترید، مناسب‌بودن کشور و شرایط IP همان پلن را از پشتیبانی تأیید بگیر.\n"
        "💬 اگر تست روی اینترنتت وصل نشد، قبل از خرید از راهنمای اتصال و پشتیبانی ربات کمک بگیر.\n"
        "🤖 خرید و قیمت نهایی فقط در @pvnetwork_bot؛ دریافت راهنما شرط عضویت یا خرید ندارد."
    )


def keyboard():
    return InlineKeyboard(
        [
            [{"text": "🛒 خرید اشتراک شخصی و مشاهده قیمت", "url": PURCHASE_URL, "style": "success"}],
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
            "version": "20261015",
            "observation": "user_request_not_price_view_or_checkout",
        },
    )
    session.commit()
    return (body(variant, need), keyboard()) if created else None


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

