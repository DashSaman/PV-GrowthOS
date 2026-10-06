"""Telegram webhook: bot starts (deep links → attribution/referral/partner),
exclusive-claim callbacks, rating callbacks. Secret-path verified; refuses
traffic when the bot token is not configured."""

from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

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
    from pv_growth.telegram.client import InlineKeyboard

    rows = [[{"text": "🛒 خرید و تعرفه‌ها", "url": "https://t.me/pvnetwork_bot"}]]
    if include_free:
        rows.append([{"text": "🎁 کانفیگ رایگان", "url": "https://t.me/pvnetwork_freeconfig"}])
    return InlineKeyboard(rows)


def _free_context(start_param: str | None) -> str | None:
    from pv_growth.free_config.membership import valid_context

    if not start_param or not start_param.startswith("freecfg_"):
        return None
    param = start_param[len("freecfg_") :]
    if param == "pv_daily_lottery":
        context = "lottery:pv_daily_lottery"
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
    from pv_growth.telegram.client import InlineKeyboard

    if context.startswith("community:"):
        _safe_send(
            client,
            user.telegram_user_id,
            "این پیشنهاد دیگر در دسترس نیست.",
            _purchase_keyboard(include_free=False),
        )
        return
    if not _free_audience_verified(session, settings, user.id):
        from pv_growth.free_config.sales_copy import main_service_pitch

        _safe_send(
            client,
            user.telegram_user_id,
            main_service_pitch(include_trial=False),
            _purchase_keyboard(include_free=False),
        )
        return
    if not _membership_ready(client, settings, user, context):
        return
    if context == "lottery:pv_daily_lottery":
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


def _handle_message(client, message: TgMessage) -> None:
    settings = get_settings()
    text = (message.text or "").strip()
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

    with session_scope(settings) as session:
        user, created = get_or_create_user(
            session,
            telegram_user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            language_code=tg_user.language_code,
            telegram_chat_id=message.chat.get("id"),
        )
        if text.startswith("/start"):
            from pv_growth.free_config.audience import decision_for_user

            free_allowed = decision_for_user(session, settings, user.id).eligible
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
            context = _free_context(start_param)
            if context is not None:
                _free_dialogue(client, session, settings, user, context)
                return
            if start_param and start_param.startswith("freecfg_"):
                _safe_send(
                    client, chat_id, "این پیشنهاد دیگر در دسترس نیست.", _purchase_keyboard(include_free=False)
                )
                return
            from pv_growth.free_config.sales_copy import main_service_pitch

            _safe_send(
                client,
                message.chat.get("id"),
                main_service_pitch(include_trial=free_allowed),
                _purchase_keyboard(include_free=free_allowed),
            )
        elif text.startswith("/help"):
            from pv_growth.free_config.audience import decision_for_user
            from pv_growth.free_config.sales_copy import main_service_pitch

            free_allowed = decision_for_user(session, settings, user.id).eligible
            _safe_send(
                client,
                message.chat.get("id"),
                main_service_pitch(include_trial=free_allowed),
                _purchase_keyboard(include_free=free_allowed),
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
    from pv_growth.core.flags import FlagService
    from pv_growth.events.service import get_or_create_user

    flags = FlagService(settings)
    with session_scope(settings) as session:
        user, _ = get_or_create_user(
            session, telegram_user_id=callback.from_.id, telegram_chat_id=callback.from_.id
        )
        from pv_growth.free_config.membership import valid_context

        if data.startswith("memberships:"):
            context = data[len("memberships:") :]
            if valid_context(context):
                _free_dialogue(client, session, settings, user, context)
            _safe_answer(client, callback.id, "عضویت و شرایط دریافت دوباره بررسی شد.")
            return
        if data.startswith(("shared:", "community:")):
            if not valid_context(data):
                _safe_answer(client, callback.id, "این پیشنهاد دیگر در دسترس نیست.")
                return
            if data.startswith("community:"):
                _safe_answer(client, callback.id, "این پیشنهاد دیگر در دسترس نیست.")
                return
            if not _free_audience_verified(session, settings, user.id):
                _safe_answer(client, callback.id, "شرایط خرید ۴۵ روز اخیر و سرویس فعال شما تأیید نشد.")
                return
            if not _membership_ready(client, settings, user, data):
                _safe_answer(client, callback.id, "عضویت در هر دو کانال باید تأیید شود.")
                return
            from pv_growth.free_config.gated_delivery import deliver_config

            try:
                outcome = deliver_config(session, settings, flags, client, user_id=user.id, context=data)
            except ValidationError as exc:
                _safe_answer(client, callback.id, str(exc))
                return
            label = {
                "delivered": "کانفیگ در گفت‌وگوی خصوصی ارسال شد.",
                "already_delivered": "درخواست این کانفیگ قبلاً ثبت شده؛ دوباره ارسال نمی‌شود.",
                "unavailable": "این کانفیگ فعلاً قابل دریافت نیست؛ کانفیگ‌های تازه را بررسی کنید.",
            }[outcome]
            _safe_answer(client, callback.id, label)
            return
        if data.startswith(("lottery:", "claim:")):
            if not valid_context(data):
                _safe_answer(client, callback.id, "درخواست نامعتبر است.")
                return
            if not _free_audience_verified(session, settings, user.id):
                _safe_answer(client, callback.id, "شرایط خرید ۴۵ روز اخیر و سرویس فعال شما تأیید نشد.")
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
                _safe_answer(client, callback.id, str(exc))
                return
            label = "ثبت شد" if is_new else "قبلاً ثبت شده"
            _safe_answer(
                client, callback.id, f"{label}؛ قرعه‌کشی {entry.draw_date.isoformat()} ساعت ۲۳ تهران."
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
