"""Owner-approved black/gold MIME replies; this module cannot send mail."""

import hashlib
import html
import re
from email.message import EmailMessage
from pathlib import Path

from pv_growth.acquisition.reply_ledger import CAMPAIGN, INTENTS, routes_for, source_url
from pv_growth.acquisition.retail_catalogue import catalogue_text, payment_text, validate_catalogue

OWNER = "pvnetwork8@gmail.com"
ASSETS = Path(__file__).with_name("mail_assets")


def _message_id(value):
    return isinstance(value, str) and re.fullmatch(r"<[^\s<>@]{1,160}@[^\s<>@]{1,90}>", value)


def build_reply(
    *,
    route,
    recipient,
    name,
    subject,
    intent,
    inbound_id,
    in_reply_to,
    references,
    catalogue,
    now,
    campaign=CAMPAIGN,
):
    """Build a single-recipient reply from verified public facts and fixed copy."""
    live = validate_catalogue(catalogue, now=now)
    if (
        route not in routes_for(campaign)
        or intent not in INTENTS
        or live is None
        or not isinstance(recipient, str)
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9][A-Za-z0-9.-]+", recipient)
        or not isinstance(subject, str)
        or not 1 <= len(subject) <= 200
        or any(ord(c) < 32 or ord(c) == 127 for c in subject)
        or not _message_id(in_reply_to)
        or not isinstance(references, list)
        or len(references) > 10
        or any(not _message_id(value) for value in references)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", inbound_id)
        or not isinstance(name, str)
        or len(name) > 160
    ):
        raise ValueError("unsafe or unavailable reply inputs")
    guide = payment_text(live)
    primary = live["recommended"]
    if intent == "price":
        answer = catalogue_text(live)
    elif intent in {"payment", "payment_reported"}:
        answer = guide
    elif intent == "setup":
        answer = (
            "برای راهنمایی اتصال، مدل گوشی و نام اینترنتی را که استفاده می‌کنید بفرمایید. "
            "پیش از پرداخت، سازگاری سرویس را با پشتیبانی همان ربات بررسی کنید."
        )
    elif intent == "collaboration":
        answer = (
            "برای بررسی همکاری، نوع فعالیت، نیاز مصرفی و نوع و زمان معرفی پیشنهادی را بفرمایید. "
            "شرایط نهایی همکاری پس از بررسی با پشتیبانی هماهنگ می‌شود."
        )
    else:
        answer = "ممنون از علاقه‌مندی شما. از دکمهٔ زیر پلن‌ها را ببینید و خرید را ادامه دهید."
        if primary:
            answer += (
                f" شروع پیشنهادی فعلی: {primary['volume_gb']} گیگابایت، "
                f"{primary['duration_days']} روز، قیمت پایه {primary['price_toman']:,} تومان. "
                "موجودی و مبلغ نهایی همان فاکتور در ربات ملاک است."
            )
    step = (
        "در ربات، پلن و فاکتور را بررسی کنید. پرداخت و تأیید وجه با پشتیبانی است. "
        "پس از تأیید و شارژ کیف پول، به خرید برگردید و خرید سرویس را تکمیل کنید. "
        "سرویس را از «سرویس‌های من» دریافت کنید."
    )
    if intent == "payment_reported":
        step = (
            "رسید را فقط از مسیر پشتیبانی رسمی داخل ربات پیگیری کنید؛ "
            "اگر نتیجه مشخص نیست، دوباره پرداخت نکن. تأیید وجه با پشتیبانی است."
        )
    entry_url = source_url(campaign, route)
    greeting = "سلام" + (" " + name.strip() if name.strip() else "")
    replacements = {
        "GREETING_TEXT": html.escape(greeting),
        "REPLY_PARAGRAPHS_HTML": "<p>" + html.escape(answer).replace("\n", "<br>") + "</p>",
        "NEXT_STEP_PARAGRAPHS_HTML": "<p>" + html.escape(step) + "</p>",
        "SOURCE_URL": html.escape(entry_url, quote=True),
        "CTA_TEXT": "انتخاب پلن و ادامهٔ خرید",
        "CTA_NOTE_TEXT": "ورود از این لینک، مسیر آشنایی شما را ثبت می‌کند؛ تأیید پرداخت با پشتیبانی است.",
    }
    body = (ASSETS / "reply.email.html").read_text(encoding="utf-8")
    for key, value in replacements.items():
        body = body.replace("{{" + key + "}}", value)
    if "{{" in body or "}}" in body:
        raise ValueError("unresolved reply placeholder")
    mail = EmailMessage()
    mail["From"] = "PV Network <" + OWNER + ">"
    mail["To"] = recipient
    mail["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    mail["In-Reply-To"] = in_reply_to
    mail["References"] = " ".join(dict.fromkeys([*references, in_reply_to]))
    identity = route + ":" + inbound_id + ":" + intent
    if campaign != CAMPAIGN:
        identity = campaign + ":" + identity
    digest = hashlib.sha256(identity.encode()).hexdigest()
    mail["Message-ID"] = "<growth-" + digest + "@pvnetwork.invalid>"
    mail["Auto-Submitted"] = "auto-replied"
    mail["X-Auto-Response-Suppress"] = "All"
    mail.set_content(greeting + "\n\n" + answer + "\n\n" + step + "\n\n" + entry_url)
    mail.add_alternative(body, subtype="html")
    mail.get_payload()[-1].add_related(
        (ASSETS / "pvnetwork-logo.png").read_bytes(),
        maintype="image",
        subtype="png",
        cid="<pvnetwork-logo>",
        disposition="inline",
        filename="pvnetwork-logo.png",
    )
    return mail
