"""Telegram webhook: bot starts (deep links → attribution/referral/partner),
exclusive-claim callbacks, rating callbacks. Secret-path verified; refuses
traffic when the bot token is not configured."""

from __future__ import annotations

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
    expected = settings.telegram_webhook_secret or "dev"
    if secret != expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    client = _telegram()
    if client is None:
        # token is a documented blocker; webhook stays inert until configured
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "telegram not configured")

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
        log.warning("telegram reply failed (isolated)", chat_id=chat_id,
                    error=str(exc)[:120])


def _handle_message(client, message: TgMessage) -> None:
    settings = get_settings()
    text = (message.text or "").strip()
    tg_user = message.from_

    from pv_growth.attribution.service import attribute
    from pv_growth.events.service import get_or_create_user, ingest
    from pv_growth.partners import service as partners
    from pv_growth.referrals import service as referrals

    with session_scope(settings) as session:
        user, created = get_or_create_user(
            session, telegram_user_id=tg_user.id, username=tg_user.username,
            first_name=tg_user.first_name, language_code=tg_user.language_code,
            telegram_chat_id=message.chat.get("id"),
        )
        if text.startswith("/start"):
            start_param = text.split(" ", 1)[1].strip() if " " in text else None
            ingest(session, "BOT_STARTED", user_id=user.id,
                   idempotency_key=f"botstart:{tg_user.id}:{user.created_at:%Y%m%d%H}")
            attribute(session, user.id, start_param)
            referrals.handle_bot_start(session, user.id, start_param)
            partners.handle_bot_start(session, user.id, start_param)
            if start_param and start_param.startswith("freecfg_"):
                from pv_growth.telegram.client import InlineKeyboard

                client.send_message(
                    message.chat.get("id"),
                    "🎁 کانفیگ رایگان PV Network آماده است!\nروی دکمه بزن تا دریافت کنی:",
                    InlineKeyboard([[
                        {"text": "🚀 دریافت کانفیگ",
                         "callback_data": f"claim:{start_param[len('freecfg_')]}"},
                    ]]),
                )
            else:
                client.send_message(
                    message.chat.get("id"),
                    "سلام! به PV Network خوش آمدید.\n"
                    "برای دریافت کانفیگ رایگان از کانال ما سر بزنید یا /help را بزنید.",
                )
        elif text.startswith("/claim"):
            parts = text.split()
            if len(parts) == 2:
                _handle_callback(client, TgCallback(
                    id=str(message.message_id), from_=tg_user,
                    data=f"claim:{parts[1]}"))
            else:
                _safe_send(client, message.chat.get("id"), "استفاده: /claim <کد کمپین>")


def _handle_callback(client, callback: TgCallback) -> None:
    settings = get_settings()
    data = callback.data or ""
    from pv_growth.core.flags import FlagService
    from pv_growth.events.service import get_or_create_user

    flags = FlagService(settings)
    with session_scope(settings) as session:
        user, _ = get_or_create_user(session, telegram_user_id=callback.from_.id,
                                     telegram_chat_id=callback.from_.id)
        if data.startswith("claim:"):
            campaign_code = data.split(":", 1)[1]
            from pv_growth.free_config.exclusive import (
                HttpProvisioningClient,
                claim_exclusive,
            )
            try:
                provisioning = HttpProvisioningClient(settings)
            except NotConfigured:
                # provisioning endpoint not provided yet (BLOCKERS.md) — the
                # claim still records and stays queued, never silently fakes
                provisioning = None
            try:
                if provisioning is None:
                    class _QueuedProvisioning:
                        def create_temp_service(self, **kwargs):
                            raise RuntimeError("provisioning endpoint not configured")

                    provisioning = _QueuedProvisioning()
                claim, is_new = claim_exclusive(
                    session, settings, flags, provisioning,
                    campaign_code=campaign_code, user_id=user.id,
                )
            except ValidationError as exc:
                client.answer_callback_query(callback.id, str(exc))
                return
            if claim.status == "active" and claim.config_payload.get("config_uri"):
                client.send_message(
                    callback.from_.id,
                    f"کانفیگ اختصاصی شما آماده است:\n{claim.config_payload['config_uri']}\n"
                    f"حجم: {claim.traffic_gb} گیگ · اعتبار: {claim.validity_hours} ساعت",
                )
            else:
                client.answer_callback_query(callback.id, "در حال آماده‌سازی، چند لحظه صبر کنید")
        elif data.startswith("rate:"):
            parts = data.split(":")  # rate:<window>:<n>
            if len(parts) == 3 and parts[2].isdigit():
                from pv_growth.feedback import service as feedback
                rating = feedback.submit_rating(
                    session, user_id=user.id, rating=int(parts[2]), window_key=parts[1])
                route = feedback.route(session, rating)
                client.answer_callback_query(callback.id, "ثبت شد، سپاسگزاریم!")
                if route == "testimonial_invite":
                    client.send_message(
                        callback.from_.id,
                        "خوشحالیم که راضی بودید! اجازه می‌دهید نظر شما را (ناشناس) منتشر کنیم؟ "
                        "با ثبت رضایت در پیام بعدی اعلام کنید.",
                    )
