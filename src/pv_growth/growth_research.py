"""Bounded daily business research; deliberately has no outreach/publishing path."""

import re
from copy import deepcopy
from datetime import UTC, timedelta
from html import escape
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from pv_growth.database.models import AppConfig
from pv_growth.database.types import utcnow
from pv_growth.free_config.shared_public import TEHRAN, local_day
from pv_growth.jobs import service as jobs
from pv_growth.jobs.runner import handler

PREFIX = "growth-research:daily:"
DIRECTORY = "https://api.torob.com/v4/internet-shop/list/"
TERMS = ("زیورآلات", "پوشاک", "لوازم خانه", "قهوه", "هنر", "کتاب", "لوازم تحریر", "کیف")
LIMIT_BYTES = 1024 * 1024
PARTNER_SEEDS = (
    ("sunira_accessories", "سانیرا اکسسوری", 1596),
    ("la_luna_collezione", "لونا", 7057),
    ("parima.accessoriess", "پریما اکسسوری", 6361),
    ("dastyarehjewellery", "دستیاره", None),
    ("dreamyhomestore", "خانه رویایی", None),
    ("zhirankids", "ژیران", None),
    ("suhara.jewelry", "سوهارا", None),
)


def clean(value, size=100):
    return re.sub(r"[<>\x00-\x1f\x7f]", " ", str(value or "")).strip()[:size]


def enabled(session):
    session.flush()
    control = session.get(AppConfig, "growth_research_control")
    pause = session.get(AppConfig, "pv_free_growth_policy")
    return not (control and control.value.get("enabled") is False) and not (
        pause and pause.value.get("operational_pause") is True
    )


def enqueue_daily(session, now):
    local = now.replace(tzinfo=UTC).astimezone(TEHRAN)
    if enabled(session) and (local.hour, local.minute) >= (7, 55):
        day = local.date().isoformat()
        jobs.enqueue(
            session,
            "growth.research_daily",
            {"day": day},
            idempotency_key=f"growth_research:{day}",
            max_attempts=1,
            priority=10,
        )


def public_website(raw):
    value = str(raw or "").strip()
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 80, 443}
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", host)
            or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid"))
        ):
            return None
        return f"https://{host}/"
    except ValueError:
        return None


def read_directory(client, url):
    # URL is built locally from one fixed HTTPS origin. Redirects never followed.
    with client.stream(
        "GET",
        url,
        timeout=10,
        follow_redirects=False,
        headers={"User-Agent": "PVBusinessResearch/1.0", "Accept": "application/json"},
    ) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > LIMIT_BYTES:
                raise ValueError("response_limit")
            body.extend(chunk)
        import json

        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("source_contract_changed")
        return data["results"][:24]


def seed_partners(day):
    # Observed on the real public profiles, not inferred from a directory count.
    current = day == "2026-10-16"
    return [
        {
            "handle": handle,
            "brand": brand,
            "source_url": f"https://www.instagram.com/{handle}/",
            "followers": count,
            "followers_observed_day": "2026-10-16" if count else None,
            "below_20k_verified": current and count is not None and count < 20000,
            "action": "check_existing_conversation_and_current_profile_before_any_contact",
            "consent_or_message_receipt_inferred": False,
        }
        for handle, brand, count in PARTNER_SEEDS
    ]


@handler("growth.research_daily")
def collect_daily(session, settings, payload, client=None):
    now = utcnow()
    day = local_day(now).isoformat()
    if payload.get("day") != day or not enabled(session):
        return
    key = PREFIX + day
    if session.get(AppConfig, key) is not None:
        return
    report = {
        "day": day,
        "observed_at_utc": now.isoformat() + "Z",
        "status": "running",
        "complete": False,
        "businesses": [],
        "sources": [],
        "failures": [],
        "partners": seed_partners(day),
        "explore_status": "official_source_not_connected",
        "trend_status": "directory_baseline_not_instagram_popularity",
        "automated_cold_dm": False,
        "customer_effects": 0,
    }
    row = AppConfig(key=key, value=deepcopy(report))
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return
    session.commit()  # Durable day reservation: a crash cannot repeat source requests.
    own_client = client is None
    client = client or httpx.Client(trust_env=False)
    found = {}
    terms = [TERMS[(local_day(now).toordinal() + i) % len(TERMS)] for i in range(4)]
    try:
        for term in terms:
            params = {
                "page": local_day(now).toordinal() % 5,
                "size": 24,
                "q": term,
                "shop_type": "online",
                "show_blocks": "false",
                "has_payment": "false",
            }
            url = DIRECTORY + "?" + urlencode(params)
            source = {"url": url, "category": term, "status": "pending"}
            report["sources"].append(source)
            try:
                results = read_directory(client, url)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                source["status"] = "failed"
                report["failures"].append({"category": term, "error_class": type(exc).__name__})
                break  # No challenge retry or provider/proxy fallback.
            source["status"] = "ok"
            for item in results:
                if not isinstance(item, dict) or item.get("block_partner") is True:
                    continue
                identifier = str(item.get("id", ""))
                website = public_website(item.get("domain"))
                if not re.fullmatch(r"\d{1,20}", identifier) or not item.get("name") or not website:
                    continue
                found.setdefault(
                    identifier,
                    {
                        "id": identifier,
                        "brand": clean(item["name"], 80),
                        "website": website,
                        "city": clean(item.get("city"), 40),
                        "category": term,
                        "source_url": f"https://torob.com/shop/{identifier}/",
                        "followers": None,
                        "below_20k_verified": False,
                        "reason": "public_online_store_needs_individual_fit_and_contact_verification",
                    },
                )
    finally:
        if own_client:
            client.close()
    # Deterministic bounded shortlist; it is neither popularity nor permission to send.
    report["businesses"] = list(found.values())[:20]
    report["observed_businesses"] = len(found)
    report["complete"] = len(report["sources"]) == 4 and not report["failures"]
    report["status"] = "ok" if report["complete"] else ("partial" if found else "unavailable")
    report["content_suggestion"] = (
        "یک ویدئوی کوتاه از راه‌اندازی واقعی اتصال با حذف کانفیگ و اطلاعات حساب؛ "
        "سپس توضیح حجم/مدت مشخص و راه خرید. نتیجهٔ واقعی را نمایش بده؛ قول اتصال همیشگی نده."
    )
    row.value = dict(report)
    cutoff = PREFIX + (local_day(now) - timedelta(days=29)).isoformat()
    session.execute(delete(AppConfig).where(AppConfig.key.startswith(PREFIX), AppConfig.key < cutoff))
    session.commit()


def report_text(session, day=None):
    day = day or local_day(utcnow()).isoformat()
    row = session.get(AppConfig, PREFIX + day)
    if not row:
        return f"🔎 تحقیق رشد · {day}\nگزارش امروز ناموجود است؛ نبود گزارش به معنی صفر فروشگاه یا فروش نیست."
    data = row.value
    lines = [
        f"🔎 تحقیق رشد · {escape(day)}",
        f"زمان مشاهده UTC: {escape(data.get('observed_at_utc', 'نامعلوم'))}",
    ]
    if not data.get("complete"):
        lines.append("⚠️ دادهٔ منبع ناموجود/ناقص است؛ نتیجهٔ کامل یا صفر قطعی نیست.")
    lines.append(
        f"فروشگاه عمومی مشاهده‌شده: {data.get('observed_businesses', 0)} · نمایش حداکثر ۵ پیشنهاد بررسی:"
    )
    for item in data.get("businesses", [])[:5]:
        lines.append(
            f"• {escape(item['brand'])} · {escape(item['city'])} · {escape(item['category'])}\n"
            f"{item['source_url']}"
        )
    lines.append("🤝 پیج‌های اولیه؛ عدد قدیمی را وضعیت امروز فرض نکن:")
    for item in data.get("partners", [])[:3]:
        count = item["followers"]
        lines.append(
            f"• @{item['handle']} · دنبال‌کننده {count if count is not None else 'نامعلوم'} · "
            f"مشاهده {item['followers_observed_day'] or 'ناموجود'}"
        )
    lines.append("📌 " + data.get("content_suggestion", "پیشنهاد محتوا هنوز آماده نیست."))
    lines.append(
        "اکسپلور/ترند اینستاگرام به منبع رسمی متصل نیست؛ رتبه یا مشاهدهٔ فهرست، محبوبیت نیست. "
        "ارسال سرد خودکار فعال نیست. قبل تماس، سابقهٔ پیام و تناسب بررسی شود؛ پیشنهاد، توافق یا فروش نیست. "
        "پشتیبانی تضمین نبود اختلال یا معروف‌شدن نیست."
    )
    return "\n\n".join(lines)[:3800]


def nightly_summary(session, day):
    row = session.get(AppConfig, PREFIX + day)
    if not row:
        return "🔎 تحقیق رشد امروز ناموجود است؛ گزارش کامل از /manage. نبود گزارش، صفر فروشگاه یا فروش نیست."
    data = row.value
    status = "کامل" if data.get("complete") else "ناقص/ناموجود"
    return (
        f"🔎 تحقیق رشد {day}: {status} · {data.get('observed_businesses', 0)} فروشگاه مشاهده‌شده.\n"
        "پیشنهادها و منابع: /manage ← تحقیق رشد. اتصال رسمی اکسپلور و ارسال سرد خودکار آماده نیست. "
        "مشاهدهٔ فهرست یا پیشنهاد همکاری، فروش نیست."
    )[:550]


def outreach_text(brand):
    return (
        f"سلام به تیم {clean(brand, 60)}، وقت بخیر 🌿\n"
        "ما PV Network هستیم؛ ارائه‌دهندهٔ اشتراک شخصی VPN برای استفادهٔ روزمره و مدیریت پیج.\n\n"
        "🤝 پیشنهاد همکاری\n"
        "ماهانه یک اشتراک VPN شخصی ۵۰ گیگی برای مدیر پیج، همراه با راهنمای راه‌اندازی و پشتیبانی اتصال؛ "
        "در مقابل، معرفی شفاف PV Network به مخاطبان پیج شما.\n\n"
        "📌 شروع همکاری\n"
        "نوع و تعداد استوری یا پست و زمان‌بندی معرفی را پیش از شروع با هم مشخص می‌کنیم. "
        "سرویس پس از توافق و بررسی سازگاری اتصال ارائه می‌شود.\n\n"
        "💬 اگر مایلید، همین‌جا «همکاری» پاسخ دهید تا جزئیات را هماهنگ کنیم. "
        "اگر تمایلی ندارید، پیگیری نمی‌کنیم.\n\nآشنایی با سرویس: https://t.me/pvnetwork_bot"
    )
