"""Public catalogue snapshots from the owner's local, read-only Mirza exporter."""

import re
from datetime import UTC, datetime, timedelta

from pv_growth.database.models import AppConfig
from pv_growth.database.types import utcnow

KEY = "retail-catalogue:active"


def validate_catalogue(value, *, now=None):
    now = now or utcnow()
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    try:
        if not isinstance(value, dict) or value.get("source") != "mirza_local_readonly":
            return None
        observed = datetime.fromisoformat(value["observed_at_utc"].replace("Z", "+00:00"))
        if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(minutes=90):
            return None
        plans, payment = value["plans"], value["payment"]
        if not isinstance(plans, list) or not 1 <= len(plans) <= 100:
            return None
        clean = []
        for p in plans:
            for k in ("code", "name", "location"):
                if not isinstance(p[k], str) or not 1 <= len(p[k]) <= 160 or any(c in p[k] for c in "<>\r\n"):
                    return None
            for k, cap in [("volume_gb", 10000), ("duration_days", 3650), ("price_toman", 10**9)]:
                if type(p[k]) is not int or not (0 if k == "duration_days" else 1) <= p[k] <= cap:
                    return None
            clean.append(
                {k: p[k] for k in ("code", "name", "location", "volume_gb", "duration_days", "price_toman")}
            )
        if len({p["code"] for p in clean}) != len(clean):
            return None
        if type(payment["direct_buy"]) is not bool or type(payment["card_to_support"]) is not bool:
            return None
        minimum = payment["minimum_wallet_toman"]
        if type(minimum) is not int or not 1 <= minimum <= 10**9:
            return None
        support = payment.get("support_handle", "")
        if support and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", support):
            return None
        clean.sort(key=lambda p: (p["price_toman"], p["volume_gb"], p["code"]))
        eligible = [p for p in clean if payment["direct_buy"] or p["price_toman"] >= minimum]
        return {
            "source": "mirza_local_readonly",
            "observed_at_utc": observed.isoformat(),
            "plans": clean,
            "recommended": eligible[0] if eligible else None,
            "payment": {
                "direct_buy": payment["direct_buy"],
                "minimum_wallet_toman": minimum,
                "card_to_support": payment["card_to_support"],
                "support_handle": support,
            },
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def current_catalogue(session, *, now=None):
    row = session.get(AppConfig, KEY)
    return validate_catalogue(row.value, now=now) if row else None


def catalogue_text(live):
    body = "📦 پلن‌های پایهٔ مشتری عادی · خوانده‌شده از میرزا\n"
    primary = live.get("recommended")
    if primary:
        body += (
            f"⭐ شروع پیشنهادی: {primary['volume_gb']} گیگ · {primary['duration_days']} روز · "
            f"{primary['price_toman']:,} تومان\n"
        )
    for p in live["plans"][:12]:
        duration = f"{p['duration_days']} روز" if p["duration_days"] else "بدون محدودیت زمانی"
        body += f"• {p['volume_gb']} گیگ · {duration} · {p['price_toman']:,} تومان\n"
    body += "قیمت پایه است؛ تخفیف اختصاصی، موجودی و مبلغ فاکتور همان لحظه در میرزا ملاک است.\n"
    payment = live["payment"]
    if not payment["direct_buy"]:
        body += (
            f"💳 خرید با کیف پول؛ حداقل شارژ عمومی {payment['minimum_wallet_toman']:,} تومان است. "
            "پلن ارزان‌تر لزوماً حداقل شارژ را کم نمی‌کند.\n"
        )
    body += "پس از تأیید پرداخت و شارژ کیف پول، خرید سرویس را تکمیل کن.\n"
    return body


def payment_text(live):
    if live is None:
        return (
            "💳 قیمت و روش پرداختِ تازه برای نمایش موجود نیست. در @pvnetwork_bot پلن و فاکتور را "
            "ببین و فقط روش نمایش‌داده‌شدهٔ همان ربات را استفاده کن. پس از شارژ، "
            "خرید سرویس را تکمیل کن. در ابهام دوباره پرداخت نکن؛ از پشتیبانی همان ربات بپرس."
        )
    payment = live["payment"]
    body = "💳 خرید تا پرداخت، با تنظیم فعلی میرزا\n\n"
    if not payment["direct_buy"]:
        body += (
            f"۱️⃣ پلن را در @pvnetwork_bot انتخاب کن. حداقل شارژ عمومی کیف پول "
            f"{payment['minimum_wallet_toman']:,} تومان است.\n"
            "۲️⃣ اگر موجودی کم است، مبلغ شارژ را وارد و روش موجود در همان ربات را انتخاب کن.\n"
        )
    else:
        body += "۱️⃣ پلن و مبلغ فاکتور را در @pvnetwork_bot بررسی کن و روش پرداخت موجود را انتخاب کن.\n"
    if payment["card_to_support"] and payment["support_handle"]:
        body += (
            f"💬 دکمهٔ کارت‌به‌کارتِ ربات، در صورت مجازبودن برای حسابت، به "
            f"@{payment['support_handle']} می‌رود؛ جزئیات و تأیید پرداخت را همان‌جا پیگیری کن. "
            "تأیید فوری یا شارژ خودکار را فرض نکن.\n"
        )
    body += (
        "۳️⃣ بعد از تأیید و افزایش موجودی، به خرید برگرد و خرید سرویس را تکمیل کن؛ "
        "شارژ کیف پول به‌تنهایی خرید سرویس نیست.\n"
        "۴️⃣ سرویس را از «سرویس‌های من» دریافت کن؛ سپس «وضعیت خرید من» و راهنمای اتصال را ببین.\n\n"
        "⚠️ اگر وجه کم شد و نتیجه مشخص نیست، دوباره پرداخت نکن؛ رسید را فقط به پشتیبانی رسمی بده. "
        "رمز، کد ورود و اطلاعات کارت را اینجا نفرست."
    )
    return body
