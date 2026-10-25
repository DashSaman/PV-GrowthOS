"""Telegram webhook: bot starts (deep links → attribution/referral/partner),
exclusive-claim callbacks, rating callbacks. Secret-path verified; refuses
traffic when the bot token is not configured."""

from __future__ import annotations

import hashlib
import hmac
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from pv_growth.acquisition.retail_offer import PURCHASE_URL
from pv_growth.api.deps import rate_limit_webhook
from pv_growth.core.config import get_settings
from pv_growth.core.errors import NotConfigured, ValidationError
from pv_growth.core.logging import get_logger
from pv_growth.database.base import session_scope

router = APIRouter(dependencies=[Depends(rate_limit_webhook)])
log = get_logger("webhook")


class TgUser(BaseModel):
    id: int
    username: str | None = None
    first_name: str | None = None
    language_code: str | None = None


class TgMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message_id: int = 0
    chat: dict = {}
    from_: TgUser | None = Field(default=None, alias="from")
    text: str | None = None


class TgCallback(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = ""
    from_: TgUser | None = Field(default=None, alias="from")
    data: str | None = None
    message: TgMessage | None = None


class TgUpdate(BaseModel):
    update_id: int
    message: TgMessage | None = None
    callback_query: TgCallback | None = None


def _telegram():
    from pv_growth.telegram.client import get_telegram

    settings = get_settings()
    try:
        return get_telegram(settings)
    except NotConfigured:
        return None


@router.post("/telegram/{secret}")
def telegram_webhook(secret: str, update: TgUpdate):
    settings = get_settings()
    expected = settings.telegram_webhook_secret
    if not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    client = _telegram()
    if client is None:
        # token is a documented blocker; webhook stays inert until configured
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "telegram not configured")

    process_update(update.model_dump(by_alias=True))
    return {"ok": True}


def process_update(update: dict) -> None:
    """Shared update pipeline for webhook + long-polling paths."""
    client = _telegram()
    if client is None:
        return
    parsed = TgUpdate.model_validate(update)
    if parsed.message is not None and parsed.message.from_ is not None:
        _handle_message(client, parsed.message)
    elif parsed.callback_query is not None and parsed.callback_query.from_ is not None:
        _handle_callback(client, parsed.callback_query)


def _safe_send(client, chat_id, text, keyboard=None):
    """A Telegram delivery failure must never abort event ingestion."""
    try:
        client.send_message(chat_id, text, keyboard)
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram reply failed (isolated)", chat_id=chat_id, error=str(exc)[:120])


def _safe_answer(client, callback_id: str, text: str) -> None:
    """Answering a callback must never roll back the claim transaction."""
    try:
        client.answer_callback_query(callback_id, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("callback answer failed (isolated)", error=str(exc)[:100])


def _purchase_keyboard(*, include_free: bool = True):
    from pv_growth.acquisition.entry import share_button
    from pv_growth.telegram.client import InlineKeyboard

    rows = [[{"text": "🛒 خرید و تعرفه‌ها", "url": PURCHASE_URL}]]
    if include_free:
        rows.append([{"text": "🎁 کانفیگ رایگان", "url": "https://t.me/pvnetwork_freeconfig"}])
        rows.append([share_button()])
    return InlineKeyboard(rows)


def _home_keyboard(session, settings, *, include_free: bool):
    """Expose available actions directly; every callback still enforces the gates."""
    from pv_growth.acquisition.entry import latest_shared_context, share_button
    from pv_growth.core.flags import FlagService
    from pv_growth.free_config.community import latest_context
    from pv_growth.free_config.lottery import _enabled
    from pv_growth.free_config.web_publication import direct_enabled
    from pv_growth.telegram.client import InlineKeyboard

    rows = [[{"text": "🛒 خرید و تعرفه‌ها", "url": PURCHASE_URL}]]
    if direct_enabled(session):
        rows.append([{"text": "🌐 کانفیگ وب قابل کپی در کانال", "url": "https://t.me/pvnetwork_freeconfig"}])
    if include_free:
        context = latest_shared_context(session, settings)
        if context is not None:
            rows.append([{"text": "🎁 دریافت تست ۱ گیگی سرویس اصلی", "callback_data": context}])
        community = latest_context(session, settings)
        if community:
            rows.append([{"text": "🌐 دریافت کانفیگ عمومی از وب", "callback_data": community}])
        if _enabled(session, FlagService(settings)):
            rows.append([{"text": "🎲 ثبت‌نام قرعه‌کشی", "callback_data": "lottery:pv_daily_lottery"}])
        rows.append([{"text": "🌐 کانال کانفیگ رایگان", "url": "https://t.me/pvnetwork_freeconfig"}])
        rows.append([share_button()])
    return InlineKeyboard(rows)


def _private_result(client, user, callback_id: str, text: str, keyboard=None):
    """A lasting private response, alongside the short Telegram callback notice."""
    _safe_send(client, user.telegram_user_id, text, keyboard or _purchase_keyboard(include_free=True))
    _safe_answer(client, callback_id, text[:180])


def _denied_result(client, user, callback_id: str, *, session, settings):

    _private_result(
        client,
        user,
        callback_id,
        _denial_text(session, settings, user.id),
        _bot_menu(session, settings),
    )


def _weekly_mode(session, settings):
    from pv_growth.core.flags import FlagService
    from pv_growth.free_config.weekly_gift import enabled

    return enabled(session, FlagService(settings))


def _bot_menu(session, settings):
    from pv_growth.telegram.navigation import menu_keyboard

    return menu_keyboard(weekly=_weekly_mode(session, settings))


def _weekly_dialogue(client, session, settings, user):
    from pv_growth.telegram.client import InlineKeyboard

    if not _weekly_mode(session, settings):
        _safe_send(client, user.telegram_user_id, "هدیهٔ هفتگی فعلاً فعال نیست.", _bot_menu(session, settings))
        return
    if not _membership_ready(client, settings, user, "weekly:pv_weekly_gift"):
        return
    _safe_send(
        client,
        user.telegram_user_id,
        "🎁 هدیهٔ سرویس اصلی PV Network: ۵۰ تا ۱۰۰ مگابایت\n"
        "برای همهٔ اعضای هر دو کانال، حتی خریداران و دارندگان سرویس فعال.\n"
        "هر هفت روز یک‌بار؛ اعتبار حداکثر ۲۴ ساعت یا پایان حجم.\n"
        "تا پایان بودجهٔ روزانهٔ ۵۰ گیگ؛ با تأیید دریافت، حساب پس از آزمون اتصال آماده می‌شود.",
        InlineKeyboard(
            [
                [{"text": "🎁 دریافت هدیهٔ این هفته", "callback_data": "weekly:pv_weekly_gift"}],
                [{"text": "🛒 خرید سرویس اصلی", "url": PURCHASE_URL}],
            ]
        ),
    )


def _weekly_request(client, session, settings, user, callback_id):
    from pv_growth.core.flags import FlagService
    from pv_growth.free_config.weekly_gift import reserve_weekly

    if not _membership_ready(client, settings, user, "weekly:pv_weekly_gift"):
        return
    try:
        row, new = reserve_weekly(session, settings, FlagService(settings), user_id=user.id)
        body = (
            "✅ درخواست هدیه ثبت شد؛ پس از آزمون اتصال، کانفیگ در همین گفت‌وگو تحویل می‌شود."
            if new
            else "ℹ️ در هفت روز گذشته هدیه یا جایزه برایت ثبت شده است؛ دریافت اضافه ممکن نیست.\n"
            "برای حجم بیشتر یا ادامهٔ استفاده می‌توانی از @pvnetwork_bot خرید کنی."
        )
    except ValidationError as exc:
        body = str(exc)
    _private_result(client, user, callback_id, body, _bot_menu(session, settings))


def _free_context(start_param: str | None) -> str | None:
    from pv_growth.free_config.membership import valid_context

    if not start_param or not start_param.startswith("freecfg_"):
        return None
    param = start_param[len("freecfg_") :]
    if param == "weekly_member":
        context = "weekly:pv_weekly_gift"
    elif param == "pv_daily_lottery":
        context = "lottery:pv_daily_lottery"
    elif param == "welcome_100":
        context = "gift:pv_welcome_100"
    elif match := re.fullmatch(r"shared_(\d{4}-\d{2}-\d{2})_(\d+)", param):
        context = f"shared:{match[1]}:{match[2]}"
    elif match := re.fullmatch(r"community_([1-9]\d*)", param):
        context = f"community:{match[1]}"
    else:
        context = f"claim:{param}"
    return context if valid_context(context) else None


def _free_audience_verified(session, settings, user_id: int) -> bool:
    from pv_growth.database.models import AppConfig
    from pv_growth.free_config.audience import decision_for_user

    policy = session.get(AppConfig, "free_audience_policy", populate_existing=True)
    if policy is not None:
        if not isinstance(policy.value, dict):
            return False
        days = policy.value.get("dormant_days", 45)
        if type(days) is not int or days < 45:
            return False
    try:
        decision = decision_for_user(session, settings, user_id)
        return decision.eligible and decision.reason in {"never_purchased", "dormant"}
    except Exception:
        return False


def _membership_ready(client, settings, user, context: str) -> bool:
    from pv_growth.free_config.membership import join_prompt, membership_for_user

    decision = membership_for_user(client, settings, user.telegram_user_id)
    if not decision.eligible:
        text, keyboard = join_prompt(context, reason=decision.reason)
        _safe_send(client, user.telegram_user_id, text, keyboard)
    return decision.eligible


def _free_dialogue(client, session, settings, user, context: str) -> None:
    from pv_growth.core.flags import FlagService
    from pv_growth.free_config.lottery import _enabled
    from pv_growth.free_config.shared_public import direct_shared
    from pv_growth.telegram.client import InlineKeyboard

    if context in {"weekly:pv_weekly_gift", "lottery:pv_daily_lottery"} and _weekly_mode(session, settings):
        _weekly_dialogue(client, session, settings, user)
        return
    if context.startswith("shared:") and direct_shared(session):
        _safe_send(
            client,
            user.telegram_user_id,
            "💎 تست‌های ۱ گیگ مشترک از سرویس اصلی VIP، مستقیم و قابل کپی در کانال رایگان هستند.",
            InlineKeyboard([[{"text": "📋 تست‌های سرویس اصلی", "url": "https://t.me/pvnetwork_freeconfig"}]]),
        )
        return
    if context.startswith("community:"):
        from pv_growth.free_config.community import current_config, enabled

        if (
            not enabled(session, settings)
            or current_config(session, settings, int(context.split(":")[1])) is None
        ):
            _safe_send(
                client,
                user.telegram_user_id,
                "این کانفیگ عمومی فعلاً قابل دریافت نیست.",
                _purchase_keyboard(include_free=True),
            )
            return
        from pv_growth.free_config.web_publication import direct_enabled

        if direct_enabled(session):
            _safe_send(
                client,
                user.telegram_user_id,
                "🌐 کانفیگ‌های عمومی وب مستقیم و قابل کپی در کانال رایگان هستند.\n"
                "حجم، مدت و IP کانفیگ وب تحت کنترل PV نیست.",
                InlineKeyboard(
                    [[{"text": "📋 باز کردن کانفیگ‌های وب", "url": "https://t.me/pvnetwork_freeconfig"}]]
                ),
            )
            return
    if not _free_audience_verified(session, settings, user.id):
        _safe_send(
            client,
            user.telegram_user_id,
            _denial_text(session, settings, user.id),
            _bot_menu(session, settings),
        )
        return
    if not _membership_ready(client, settings, user, context):
        return
    if context == "gift:pv_welcome_100":
        from pv_growth.free_config.welcome_gift import gift_enabled

        if not gift_enabled(session, settings, FlagService(settings)):
            _safe_send(
                client,
                user.telegram_user_id,
                "پیشنهاد اشانتیون امروز فعلاً قابل دریافت نیست.",
                _purchase_keyboard(include_free=False),
            )
            return
        text = (
            "🎁 اشانتیون امروز: ۱۰۰ مگابایت از پلن تانل اصلی PV Network\n"
            "⏳ اعتبار حداکثر ۲۴ ساعت یا پایان حجم؛ یک بار برای هر نفر.\n"
            "فقط برای کاربران ربات اصلی بدون هیچ سابقهٔ خرید و بدون سرویس فعال، با عضویت در هر دو کانال.\n"
            "برای دریافت روی دکمه بزنید."
        )
        label = "🎁 دریافت اشانتیون ۱۰۰ مگی"
    elif context == "lottery:pv_daily_lottery":
        if not _enabled(session, FlagService(settings)):
            _safe_send(
                client,
                user.telegram_user_id,
                "قرعه‌کشی فعلاً فعال نیست.",
                _purchase_keyboard(include_free=False),
            )
            return
        text = (
            "🎲 قرعه‌کشی روزانهٔ تست اختصاصی PV Network\n"
            "برای شرکت روی دکمه بزنید؛ ثبت‌نام به معنی دریافت فوری نیست.\n"
            "قرعه‌کشی ساعت ۲۳ تهران؛ ثبت‌نام بعد از آن برای روز بعد است.\n"
            "حجم هر برنده ۵۰ تا ۱۰۲۴ مگابایت، معتبر حداکثر ۲۴ ساعت یا پایان حجم.\n"
            "عضویت در هر دو کانال الزامی است؛ فقط افراد بدون خرید در ۴۵ روز اخیر "
            "و بدون سرویس فعال واجد شرایط‌اند."
        )
        label = "🎲 شرکت در قرعه‌کشی"
    else:
        text = (
            "🎁 برای دریافت کانفیگ رایگان، دکمهٔ زیر را بزنید.\n"
            "عضویت در هر دو کانال الزامی است؛ فقط افراد بدون خرید در ۴۵ روز اخیر "
            "و بدون سرویس فعال واجد شرایط‌اند."
        )
        label = "🚀 دریافت کانفیگ"
    _safe_send(
        client, user.telegram_user_id, text, InlineKeyboard([[{"text": label, "callback_data": context}]])
    )


def _current_notice(session, settings, decision):
    if _weekly_mode(session, settings):
        return ""
    return _audience_notice(decision)


def _audience_notice(decision) -> str:
    if decision.eligible:
        return ""
    if decision.reason == "active_or_unverified_service":
        reason = "در سوابق حسابت سرویس فعال یا وضعیت سرویس تأییدنشده وجود دارد."
    elif decision.reason == "recent_purchase":
        reason = "در ۴۵ روز گذشته خرید داشته‌ای."
    else:
        reason = "سابقهٔ خریدت فعلاً قابل تأیید نیست؛ دریافت تا تأیید سوابق متوقف است."
    return (
        "ℹ️ دریافت تست سرور اصلی و ثبت‌نام قرعه‌کشی برای حسابت فعلاً مجاز نیست.\n"
        + reason
        + "\nشرط دریافت: ۴۵ روز بدون خرید، نداشتن سرویس فعال یا نامشخص و عضویت در هر دو کانال.\n"
        "بررسی شرایط با /start دوباره انجام می‌شود.\n\n"
    )


def _denial_text(session, settings, user_id):
    from pv_growth.free_config.audience import decision_for_user

    try:
        notice = _audience_notice(decision_for_user(session, settings, user_id))
    except Exception:
        notice = "ℹ️ سابقهٔ خرید و وضعیت سرویس فعلاً قابل تأیید نیست؛ تست اصلی و ثبت‌نام متوقف است.\n"
    return (notice or "ℹ️ سیاست دریافت تست اصلی و قرعه فعلاً قابل تأیید نیست.\n") + (
        "🌐 کانفیگ‌های وب عمومی در کانال قابل کپی هستند.\n"
        "برای بررسی دوباره یا انتخاب مسیر دیگر از دکمه‌های پایین استفاده کن."
    )


def _handle_message(client, message: TgMessage) -> None:
    settings = get_settings()
    text = (message.text or "").strip()
    from pv_growth.telegram.navigation import help_text, menu_command

    command = menu_command(text)
    tg_user = message.from_
    chat_id = message.chat.get("id")
    if (
        tg_user is None
        or tg_user.id <= 0
        or type(chat_id) is not int
        or chat_id != tg_user.id
        or message.chat.get("type", "private") != "private"
    ):
        return

    from pv_growth.attribution.service import attribute
    from pv_growth.events.service import get_or_create_user, ingest
    from pv_growth.partners import service as partners
    from pv_growth.referrals import service as referrals

    raw_command, _, argument = text.partition(" ")
    management_action = {
        "/manage": "menu",
        "/hours": "hours:" + argument.strip(),
        "/web_count": "web:" + argument.strip(),
        "/active": "active:" + argument.strip(),
        "/sales": "sales",
    }.get(raw_command.split("@")[0])
    if management_action is not None:
        from pv_growth.telegram.management import handle_action

        with session_scope(settings) as session:
            handle_action(
                session,
                settings,
                client,
                user_id=tg_user.id,
                chat_id=chat_id,
                chat_type="private",
                action=management_action,
                request_key=(
                    "m:" + hashlib.sha256(f"{tg_user.id}:{message.message_id}".encode()).hexdigest()[:40]
                    if message.message_id > 0
                    else None
                ),
            )
        return

    with session_scope(settings) as session:
        user, created = get_or_create_user(
            session,
            telegram_user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            language_code=tg_user.language_code,
            telegram_chat_id=message.chat.get("id"),
        )
        if command == "/start":
            from pv_growth.acquisition.post_report import START_PARAMETER, record_entry

            if argument.strip() == START_PARAMETER:
                result = record_entry(session, user, request_id=f"m:{message.message_id}")
                if result:
                    _safe_send(client, chat_id, *result)
                return
            from pv_growth.acquisition.retail_offer import SOURCES, record_request

            guide_source = (
                argument.strip().removeprefix("buy_") if argument.strip().startswith("buy_") else None
            )
            if guide_source in SOURCES:
                result = record_request(
                    session,
                    user,
                    source=guide_source,
                    request_id=f"m:{message.message_id}" if message.message_id else None,
                )
                if result:
                    _safe_send(client, chat_id, *result)
                return
            from pv_growth.acquisition.campaign_entry import record_entry as record_campaign_entry

            handled, result = record_campaign_entry(
                session,
                user,
                start_param=argument.strip(),
                request_id=f"m:{message.message_id}" if message.message_id else None,
            )
            if handled:
                if result:
                    _safe_send(client, chat_id, *result)
                return
            from pv_growth.free_config.audience import decision_for_user

            decision = decision_for_user(session, settings, user.id)
            free_allowed = decision.eligible
            start_param = text.split(" ", 1)[1].strip() if " " in text else None
            ingest(
                session,
                "BOT_STARTED",
                user_id=user.id,
                idempotency_key=f"botstart:{tg_user.id}:{user.created_at:%Y%m%d%H}",
            )
            attribute(session, user.id, start_param)
            referrals.handle_bot_start(session, user.id, start_param)
            partners.handle_bot_start(session, user.id, start_param)
            from pv_growth.acquisition.entry import is_acquisition_start, latest_shared_context

            if is_acquisition_start(start_param) and free_allowed:
                latest = latest_shared_context(session, settings)
                if latest is not None:
                    _free_dialogue(client, session, settings, user, latest)
                else:
                    _safe_send(
                        client,
                        chat_id,
                        "تست تأییدشدهٔ قابل دریافت فعلاً در دسترس نیست؛ "
                        "پیشنهاد تازه را در کانال رایگان دنبال کن.",
                        _purchase_keyboard(include_free=True),
                    )
                return
            context = _free_context(start_param)
            if context is not None:
                _free_dialogue(client, session, settings, user, context)
                return
            if start_param and start_param.startswith("freecfg_"):
                _safe_send(
                    client, chat_id, "این پیشنهاد دیگر در دسترس نیست.", _purchase_keyboard(include_free=False)
                )
                return
            _safe_send(
                client,
                message.chat.get("id"),
                "🏠 منوی اصلی PV Network\n\n"
                + _current_notice(session, settings, decision)
                + (
                    "از دکمه‌های پایین، تست اصلی، کانفیگ وب، هدیهٔ هفتگی یا خرید را انتخاب کن."
                    if _weekly_mode(session, settings)
                    else "از دکمه‌های پایین، تست اصلی، کانفیگ وب، قرعه یا خرید را انتخاب کن."
                ),
                _bot_menu(session, settings),
            )
        elif command in {"/free", "/lottery", "/web", "/gift"}:
            from pv_growth.free_config.shared_public import direct_shared
            from pv_growth.free_config.web_publication import direct_enabled

            if command in {"/gift", "/lottery"} and _weekly_mode(session, settings):
                _weekly_dialogue(client, session, settings, user)
                return
            if command == "/gift":
                _safe_send(client, chat_id, "هدیهٔ هفتگی فعلاً فعال نیست.", _bot_menu(session, settings))
                return
            if command == "/free" and direct_shared(session):
                _free_dialogue(client, session, settings, user, "shared:public")
                return
            if command == "/web" and direct_enabled(session):
                _safe_send(
                    client,
                    chat_id,
                    "🌐 کانفیگ‌های وب مستقیم و قابل کپی در کانال رایگان هستند.\n"
                    "منبع عمومی است؛ حجم، مدت و IP تحت کنترل PV نیست.",
                    _purchase_keyboard(include_free=True),
                )
                return
            if command == "/lottery":
                _free_dialogue(client, session, settings, user, "lottery:pv_daily_lottery")
            else:
                from pv_growth.acquisition.entry import latest_shared_context
                from pv_growth.free_config.community import latest_context

                context = (
                    latest_context(session, settings)
                    if command == "/web"
                    else latest_shared_context(session, settings)
                )
                if context:
                    _free_dialogue(client, session, settings, user, context)
                else:
                    _safe_send(
                        client,
                        chat_id,
                        "تست قابل دریافت فعلاً موجود نیست؛ پیشنهاد تازه را در کانال دنبال کنید.",
                        _bot_menu(session, settings),
                    )
        elif command == "/buy":
            from pv_growth.acquisition.retail_offer import record_request

            result = record_request(
                session,
                user,
                source="menu",
                request_id=f"m:{message.message_id}" if message.message_id else None,
            )
            if result:
                _safe_send(client, chat_id, *result)
        elif command in {"/share", "/referral"}:
            from pv_growth.acquisition.entry import sharing_prompt

            body, keyboard = sharing_prompt()
            _safe_send(client, chat_id, body, keyboard)
        elif command in {"/help", "/menu", "/status"}:
            from pv_growth.free_config.audience import decision_for_user

            decision = decision_for_user(session, settings, user.id)
            _safe_send(
                client,
                message.chat.get("id"),
                (
                    help_text(weekly=_weekly_mode(session, settings))
                    if command == "/help"
                    else "🏠 منوی اصلی؛ یکی از دکمه‌های پایین را انتخاب کن."
                )
                + "\n\n"
                + _current_notice(session, settings, decision),
                _bot_menu(session, settings),
            )
        elif text.startswith("/claim"):
            parts = text.split()
            if len(parts) == 2:
                _handle_callback(
                    client,
                    TgCallback(
                        id=str(message.message_id), from_=tg_user, message=message, data=f"claim:{parts[1]}"
                    ),
                )
            else:
                _safe_send(client, message.chat.get("id"), "استفاده: /claim <کد کمپین>")
        else:
            from pv_growth.free_config.audience import decision_for_user

            decision = decision_for_user(session, settings, user.id)
            allowed = decision.eligible
            _safe_send(
                client,
                chat_id,
                (
                    "برای دریافت تست، کانفیگ‌های وب یا قرعه‌کشی، یکی از دکمه‌های زیر را بزنید."
                    if allowed or _weekly_mode(session, settings)
                    else _current_notice(session, settings, decision)
                ),
                _bot_menu(session, settings),
            )


def _handle_callback(client, callback: TgCallback) -> None:
    settings = get_settings()
    data = callback.data or ""
    chat = callback.message.chat if callback.message is not None else {}
    if (
        callback.from_ is None
        or callback.from_.id <= 0
        or type(chat.get("id")) is not int
        or chat.get("id") != callback.from_.id
        or chat.get("type", "private") != "private"
    ):
        _safe_answer(client, callback.id, "این دکمه را در گفت‌وگوی خصوصی ربات استفاده کنید.")
        return
    if data.startswith("manage:"):
        from pv_growth.telegram.management import handle_action

        with session_scope(settings) as session:
            handle_action(
                session,
                settings,
                client,
                user_id=callback.from_.id,
                chat_id=chat["id"],
                chat_type="private",
                action=data[7:],
                request_key="c:" + hashlib.sha256(callback.id.encode()).hexdigest()[:40],
            )
        _safe_answer(client, callback.id, "نتیجه در همین گفت‌وگو نمایش داده شد.")
        return
    _safe_answer(client, callback.id, "در حال بررسی درخواست؛ نتیجه در همین گفت‌وگو نمایش داده می‌شود.")
    from pv_growth.core.flags import FlagService
    from pv_growth.events.service import get_or_create_user

    flags = FlagService(settings)
    with session_scope(settings) as session:
        user, _ = get_or_create_user(
            session, telegram_user_id=callback.from_.id, telegram_chat_id=callback.from_.id
        )
        from pv_growth.free_config.membership import valid_context

        if data.startswith("retail:"):
            from pv_growth.acquisition.purchase_journey import action
            from pv_growth.acquisition.retail_offer import NEEDS, selected_need

            if data[7:] not in NEEDS:
                result = action(session, settings, user, code=data[7:], request_id="c:" + callback.id)
                if result:
                    _safe_send(client, user.telegram_user_id, *result)
                return

            result = selected_need(session, user, need=data[7:], request_id="c:" + callback.id)
            if result:
                _safe_send(client, user.telegram_user_id, *result)
            return
        if data.startswith("memberships:"):
            context = data[len("memberships:") :]
            if valid_context(context):
                _free_dialogue(client, session, settings, user, context)
            _safe_answer(client, callback.id, "عضویت و شرایط دریافت دوباره بررسی شد.")
            return
        if data in {"weekly:pv_weekly_gift", "lottery:pv_daily_lottery"} and _weekly_mode(session, settings):
            _weekly_request(client, session, settings, user, callback.id)
            return
        if data == "gift:pv_welcome_100":
            from pv_growth.free_config.welcome_gift import request_gift

            try:
                outcome = request_gift(session, settings, flags, client, user_id=user.id)
            except ValidationError as exc:
                _safe_answer(client, callback.id, str(exc))
                return
            label = {
                "delivered": "اشانتیون در گفت‌وگوی خصوصی ارسال شد.",
                "already_reserved": "درخواست اشانتیون شما قبلاً ثبت شده است.",
                "unavailable": "اشانتیون امروز فعلاً قابل دریافت نیست.",
            }.get(outcome, "اشانتیون امروز فعلاً قابل دریافت نیست.")
            _safe_answer(client, callback.id, label)
            return
        if data.startswith(("shared:", "community:")):
            if not valid_context(data):
                _private_result(client, user, callback.id, "این پیشنهاد دیگر در دسترس نیست.")
                return
            from pv_growth.free_config.web_publication import direct_enabled

            if data.startswith("community:") and direct_enabled(session):
                _free_dialogue(client, session, settings, user, data)
                _safe_answer(client, callback.id, "کانفیگ وب مستقیم در کانال قابل کپی است.")
                return
            from pv_growth.free_config.shared_public import direct_shared

            if data.startswith("shared:") and direct_shared(session):
                _free_dialogue(client, session, settings, user, data)
                return
            if not _free_audience_verified(session, settings, user.id):
                _denied_result(client, user, callback.id, session=session, settings=settings)
                return
            if not _membership_ready(client, settings, user, data):
                _safe_answer(client, callback.id, "عضویت در هر دو کانال باید تأیید شود.")
                return
            from pv_growth.free_config.gated_delivery import deliver_config

            try:
                outcome = deliver_config(session, settings, flags, client, user_id=user.id, context=data)
            except ValidationError as exc:
                _private_result(client, user, callback.id, str(exc))
                return
            label = {
                "delivered": "کانفیگ در گفت‌وگوی خصوصی ارسال شد.",
                "already_delivered": "درخواست این کانفیگ قبلاً ثبت شده؛ دوباره ارسال نمی‌شود.",
                "unavailable": "این کانفیگ فعلاً قابل دریافت نیست؛ کانفیگ‌های تازه را بررسی کنید.",
            }[outcome]
            if outcome == "delivered":
                _safe_answer(client, callback.id, label)
            else:
                _private_result(client, user, callback.id, label)
            return
        if data.startswith(("lottery:", "claim:")):
            if not valid_context(data):
                _private_result(client, user, callback.id, "درخواست نامعتبر است.")
                return
            if not _free_audience_verified(session, settings, user.id):
                _denied_result(client, user, callback.id, session=session, settings=settings)
                return
            if not _membership_ready(client, settings, user, data):
                _safe_answer(client, callback.id, "عضویت در هر دو کانال باید تأیید شود.")
                return
        if data.startswith("lottery:"):
            from pv_growth.free_config.lottery import enter_lottery

            try:
                entry, is_new = enter_lottery(
                    session,
                    settings,
                    flags,
                    campaign_code=data.split(":", 1)[1],
                    user_id=user.id,
                )
                session.commit()
            except ValidationError as exc:
                _private_result(client, user, callback.id, str(exc))
                return
            label = "ثبت شد" if is_new else "قبلاً ثبت شده"
            _private_result(
                client,
                user,
                callback.id,
                f"✅ ثبت‌نام شما {label}.\n"
                f"🎲 قرعه‌کشی {entry.draw_date.isoformat()} ساعت ۲۳ تهران انجام می‌شود.\n"
                "ثبت‌نام به معنی دریافت فوری اکانت نیست؛ در صورت برنده‌شدن، کانفیگ در همین گفت‌وگو ارسال می‌شود.",
            )
        elif data.startswith("claim:"):
            campaign_code = data.split(":", 1)[1]
            from pv_growth.free_config.exclusive import claim_exclusive
            from pv_growth.provisioning import xui as _xui

            # single provisioning boundary: the existing X-UI panel API
            provisioning = _xui.get_provisioning(settings)
            try:
                if provisioning is None:

                    class _QueuedProvisioning:
                        def create_temp_service(self, **kwargs):
                            raise RuntimeError("provisioning endpoint not configured")

                    provisioning = _QueuedProvisioning()
                claim, is_new = claim_exclusive(
                    session,
                    settings,
                    flags,
                    provisioning,
                    campaign_code=campaign_code,
                    user_id=user.id,
                )
            except ValidationError as exc:
                _safe_answer(client, callback.id, str(exc))
                return
            if claim.status == "active" and claim.config_payload.get("config_uri"):
                _safe_send(
                    client,
                    callback.from_.id,
                    f"کانفیگ اختصاصی شما آماده است:\n{claim.config_payload['config_uri']}\n"
                    f"حجم: {claim.traffic_gb} گیگ · اعتبار: {claim.validity_hours} ساعت\n\n"
                    "اگر اتصال مناسب بود، سرویس اصلی را از دکمه زیر بگیرید.",
                    _purchase_keyboard(include_free=False),
                )
            else:
                _safe_answer(client, callback.id, "در حال آماده‌سازی، چند لحظه صبر کنید")
        elif data.startswith("rate:"):
            parts = data.split(":")  # rate:<window>:<n>
            if len(parts) == 3 and parts[2].isdigit():
                from pv_growth.feedback import service as feedback

                rating = feedback.submit_rating(
                    session, user_id=user.id, rating=int(parts[2]), window_key=parts[1]
                )
                route = feedback.route(session, rating)
                _safe_answer(client, callback.id, "ثبت شد، سپاسگزاریم!")
                if route == "testimonial_invite":
                    _safe_send(
                        client,
                        callback.from_.id,
                        "خوشحالیم که راضی بودید! اجازه می‌دهید نظر شما را (ناشناس) منتشر کنیم؟ "
                        "با ثبت رضایت در پیام بعدی اعلام کنید.",
                    )
