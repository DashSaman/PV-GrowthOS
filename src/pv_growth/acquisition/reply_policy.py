"""Conservative, I/O-free decisions for already verified pilot threads.

This is not a sender. The caller must also verify the original sent thread,
campaign allowlist, fresh catalogue and durable reservation before any send.
An unclear message never becomes consent merely because sales copy is quoted.
"""

import re
from email.utils import getaddresses


def _addresses(value):
    return [address.lower() for _name, address in getaddresses([value]) if address]


def _new_text(value):
    lines = []
    for line in str(value).splitlines():
        stripped = line.strip()
        if re.match(r"(?i)(on .+wrote:|[-_]{2,}\s*original message|در تاریخ.+نوشت)", stripped):
            break
        if stripped.startswith(">"):
            continue
        lines.append(stripped)
    return " ".join(lines).lower().translate(str.maketrans({"ي": "ی", "ك": "ک", "\u200c": " "}))


def reply_decision(message: dict, *, recipient: str, owner: str) -> str:
    """Return an answer intent, hold, stop or ignore; never perform an action."""
    values = {}
    for row in message.get("headers", []):
        name, value = row.get("name"), row.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return "hold_headers"
        values.setdefault(name.lower(), []).append(value)
    headers = {name: rows[0] for name, rows in values.items()}
    labels = set(message.get("labels", []))
    if labels & {"SPAM", "TRASH"}:
        return "ignore_spam"
    sender = _addresses(headers.get("from", ""))
    if sender == [owner.lower()]:
        return "ignore_self"
    auto = headers.get("auto-submitted", "no").lower().strip()
    if (
        auto != "no"
        or headers.get("precedence", "").lower().strip() in {"bulk", "list", "junk"}
        or any(name in headers for name in ("list-id", "x-autoreply", "x-autorespond"))
        or "multipart/report" in headers.get("content-type", "").lower()
        or any(re.search(r"(?i)(mailer-daemon|postmaster)", address) for address in sender)
    ):
        return "ignore_automatic"
    recipient = recipient.lower()
    if (
        sender != [recipient]
        or len(values.get("from", [])) != 1
        or (
            "reply-to" in values
            and (len(values["reply-to"]) != 1 or _addresses(headers["reply-to"]) != [recipient])
        )
    ):
        return "hold_identity"
    auth = headers.get("authentication-results", "")
    domain = re.escape(recipient.rsplit("@", 1)[-1])
    if (
        len(values.get("authentication-results", [])) != 1
        or not re.match(r"(?i)^mx\.google\.com\s*;", auth)
        or not re.search(r"(?i)(?:^|[;\s])dmarc=pass(?:\s|;)", auth)
        or not re.search(r"(?i)header\.from=" + domain + r"(?:[;\s]|$)", auth)
    ):
        return "hold_authentication"
    body = _new_text(message.get("text", ""))
    if not body or len(body) > 12000:
        return "hold_intent"
    if re.search(
        r"تمایل ندار|علاقه ندار|نمی\s*خوا|پیگیری نکن|پیام نفرست|ارسال نکن|لغو|حذف|"
        r"not interested|unsubscribe|no thanks|^نه(?:\s|$)",
        body,
    ):
        return "stop"
    if re.search(r"شاید|بعدا|بعداً|فعلا|فعلاً|الان نه|maybe|later|not now", body):
        return "hold_intent"
    if re.search(r"تخفیف|درصد|تسویه|رایگان|discount|commission|free\b", body):
        return "hold_terms"
    if re.search(r"پرداخت (?:کردم|شده)|خریدم|واریز کردم|i (?:have )?paid", body):
        return "payment_reported"
    if re.search(r"پرداخت|واریز|payment|pay\b", body):
        return "payment"
    if re.search(r"قیمت|تعرفه|پلن|price|pricing|plans?\b", body):
        return "price"
    if re.search(r"نصب|آیفون|اندروید|اینترنت|متصل|setup|android|iphone", body):
        return "setup"
    if re.search(r"همکاری|معرفی|collaborat|partner", body):
        return "collaboration"
    if re.search(r"علاقه\s*مند|تمایل دار|خرید|راهنمایی کنید|^بله(?:\s|[،.!]|$)|interested|^yes\b", body):
        return "interest"
    return "hold_intent"
