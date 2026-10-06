"""Fresh, read-only membership checks for both owner-required channels."""

import re
from dataclasses import dataclass

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import User
from pv_growth.telegram.client import InlineKeyboard, get_telegram

MAIN_CHANNEL_ID = "-1003855234264"
FREE_CHANNEL_ID = "-1004310246787"


@dataclass(frozen=True)
class MembershipDecision:
    eligible: bool
    reason: str
    missing_channels: tuple[str, ...] = ()


def membership_for_user(telegram, settings, telegram_user_id: int) -> MembershipDecision:
    if type(telegram_user_id) is not int or telegram_user_id <= 0:
        return MembershipDecision(False, "missing_identity")
    free_channel = str(settings.free_channel_id).strip()
    if free_channel not in {FREE_CHANNEL_ID, "@pvnetwork_freeconfig", "pvnetwork_freeconfig"}:
        return MembershipDecision(False, "verification_unavailable")
    missing = []
    unknown = False
    for channel in (MAIN_CHANNEL_ID, free_channel):
        try:
            row = telegram.get_chat_member(channel, telegram_user_id)
            status = row.get("status") if isinstance(row, dict) else None
            if status in {"creator", "administrator", "member"}:
                continue
            if status == "restricted" and row.get("is_member") is True:
                continue
            if status in {"left", "kicked", "restricted"}:
                missing.append(channel)
            else:
                unknown = True
        except Exception:
            unknown = True
    if unknown:
        return MembershipDecision(False, "verification_unavailable")
    return MembershipDecision(not missing, "joined" if not missing else "join_required", tuple(missing))


def require_membership(session, settings, user_id: int, *, telegram=None) -> None:
    user = session.get(User, user_id, populate_existing=True)
    if (
        user is None
        or user.is_blocked
        or type(user.telegram_user_id) is not int
        or user.telegram_user_id <= 0
        or user.telegram_chat_id != user.telegram_user_id
    ):
        raise ValidationError("دریافت رایگان فقط در گفت‌وگوی خصوصی ربات امکان‌پذیر است.")
    try:
        decision = membership_for_user(telegram or get_telegram(settings), settings, user.telegram_user_id)
    except Exception:
        decision = MembershipDecision(False, "verification_unavailable")
    if not decision.eligible:
        message = (
            "امکان تأیید عضویت فعلاً وجود ندارد؛ دسترسی ربات باید بررسی شود."
            if decision.reason == "verification_unavailable"
            else "ابتدا عضو هر دو کانال شوید و سپس بررسی عضویت را بزنید."
        )
        raise ValidationError(message)


def valid_context(context: str) -> bool:
    return bool(
        isinstance(context, str)
        and len(("memberships:" + context).encode()) <= 64
        and re.fullmatch(
            r"(?:shared:\d{4}-\d{2}-\d{2}:(?:[1-9]|10)|community:[1-9]\d*|"
            r"lottery:pv_daily_lottery|claim:[A-Za-z0-9_-]{1,40})",
            context,
        )
    )


def join_prompt(context: str, *, reason: str = "") -> tuple[str, InlineKeyboard]:
    if not valid_context(context):
        raise ValidationError("invalid membership context")
    text = (
        "🎁 برای دریافت کانفیگ رایگان و شرکت در قرعه‌کشی، عضو هر دو کانال زیر شوید.\n"
        "فقط افراد بدون خرید در ۴۵ روز اخیر و بدون سرویس فعال واجد شرایط‌اند.\n"
        "پس از عضویت، دکمهٔ بررسی عضویت را بزنید."
    )
    if reason == "verification_unavailable":
        text += "\n⚠️ امکان تأیید عضویت فعلاً وجود ندارد؛ دسترسی ربات باید بررسی شود."
    return text, InlineKeyboard(
        [
            [{"text": "📢 عضویت در کانال اصلی", "url": "https://t.me/pvnetwork0"}],
            [{"text": "🎁 عضویت در کانال رایگان", "url": "https://t.me/pvnetwork_freeconfig"}],
            [{"text": "✅ بررسی عضویت", "callback_data": "memberships:" + context}],
        ]
    )
