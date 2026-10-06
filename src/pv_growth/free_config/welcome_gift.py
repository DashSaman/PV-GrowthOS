"""One owned 100 MiB gift for a proven existing main-bot nonbuyer, today only."""

from __future__ import annotations

import html
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import Campaign, ExclusiveClaim, FreeAllocation, User
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.free_config.audience import decision_for_user
from pv_growth.free_config.budgets import MIB, cap_for, lock_day, reserve
from pv_growth.free_config.exclusive import _campaign_window_ok
from pv_growth.free_config.lottery import _panel_proof
from pv_growth.free_config.membership import require_membership
from pv_growth.free_config.owned_delivery import verified_uri
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.sales_copy import config_footer
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
from pv_growth.provisioning.guard import check_backend
from pv_growth.provisioning.xui import get_provisioning
from pv_growth.telegram.client import InlineKeyboard

CAMPAIGN_CODE = "pv_welcome_100"
TRAFFIC_BYTES = 100 * MIB
TEHRAN = timezone(timedelta(hours=3, minutes=30))
TERMINAL = {"message_reserved", "delivery_unknown", "delivered", "gate_blocked", "expired", "stale"}


def _today() -> date:
    now = utcnow()
    return (now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)).astimezone(TEHRAN).date()


def _campaign(session: Session, flags: FlagService, day: date) -> Campaign:
    session.flush()
    snapshot = flags.snapshot()
    if not snapshot["FREE_CONFIG_ENABLED"] or not snapshot["PV_EXCLUSIVE_CONFIG_ENABLED"]:
        raise ValidationError("welcome gift disabled")
    cap_for(session, day, "gift")
    campaign = session.scalar(
        select(Campaign).where(Campaign.code == CAMPAIGN_CODE).execution_options(populate_existing=True)
    )
    start = datetime.combine(day, time.min, TEHRAN).astimezone(UTC).replace(tzinfo=None)
    end = start + timedelta(days=1)
    if (
        day != _today()
        or campaign is None
        or campaign.kind != "free_config_exclusive"
        or (campaign.config or {}).get("gift") is not True
        or campaign.start_at is None
        or campaign.end_at is None
        or not start <= campaign.start_at < campaign.end_at <= end
        or not _campaign_window_ok(campaign, utcnow())
        or utcnow() >= campaign.end_at
    ):
        raise ValidationError("welcome gift campaign unavailable")
    return campaign


def gift_enabled(session: Session, settings: Settings, flags: FlagService) -> bool:
    """Read-only dialogue gate: no memberships, panel, or original-bot lookups."""
    try:
        _campaign(session, flags, _today())
        require_tunnel_plan(session, settings)
        return True
    except ValidationError:
        return False


def _require_original_user(settings: Settings, telegram_user_id: int) -> None:
    try:
        with MirzaMySQLReader(settings).connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT id,User_Status,agent,step FROM user WHERE id=%s LIMIT 1", (telegram_user_id,)
            )
            row = cursor.fetchone()
        if (
            not isinstance(row, dict)
            or str(row.get("id")) != str(telegram_user_id)
            or row.get("User_Status") != "Active"
            or row.get("agent") != "f"
        ):
            raise ValidationError("main-bot existing user not confirmed")
    except Exception as exc:
        raise ValidationError("عضویت قبلی شما در ربات اصلی خرید تأیید نشد.") from exc


def _require_audience(session: Session, settings: Settings, telegram, user_id: int) -> User:
    session.flush()
    user = session.get(User, user_id, populate_existing=True)
    if (
        user is None
        or user.is_blocked
        or type(user.telegram_user_id) is not int
        or user.telegram_user_id <= 0
        or user.telegram_chat_id != user.telegram_user_id
    ):
        raise ValidationError("هدیه فقط در گفت‌وگوی خصوصی ربات قابل دریافت است.")
    require_membership(session, settings, user_id, telegram=telegram)
    _require_original_user(settings, user.telegram_user_id)
    try:
        decision = decision_for_user(session, settings, user_id)
    except Exception as exc:
        raise ValidationError("سوابق خرید و سرویس فعال شما تأیید نشد.") from exc
    if not decision.eligible or decision.reason != "never_purchased":
        raise ValidationError("این هدیه فقط برای کاربران ربات اصلی که هرگز خرید نکرده‌اند است.")
    return user


def _locked_user(session: Session, user_id: int) -> User | None:
    return session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )


def request_gift(
    session: Session,
    settings: Settings,
    flags: FlagService,
    telegram,
    *,
    user_id: int,
    adapter=None,
) -> str:
    day = _today()
    try:
        campaign = _campaign(session, flags, day)
        binding = require_tunnel_plan(session, settings)
    except ValidationError:
        return "unavailable"
    lock_day(session, day)
    _locked_user(session, user_id)
    key = f"welcome-gift:{user_id}"  # lifetime identity; changing campaigns/dates never creates another gift
    allocation = session.get(FreeAllocation, key, populate_existing=True)
    if allocation is not None and allocation.status in TERMINAL:
        return "already_reserved"
    _require_audience(session, settings, telegram, user_id)
    if day != _today():
        return "unavailable"
    try:
        _campaign(session, flags, day)
        if allocation is None:
            allocation = reserve(
                session,
                key=key,
                day=day,
                bucket="gift",
                traffic_bytes=TRAFFIC_BYTES,
                user_id=user_id,
                campaign_id=campaign.id,
            )
            claim = ExclusiveClaim(
                claim_key=key,
                campaign_id=campaign.id,
                user_id=user_id,
                claim_date=day,
                traffic_gb=0,
                traffic_bytes=TRAFFIC_BYTES,
                validity_hours=24,
                location="auto",
                status="gift_pending",
                config_payload={"protocol": "vless", "gift": True},
            )
            session.add(claim)
            session.flush()
            allocation.claim_id = claim.id
            allocation.payload = {"gift": True, "tunnel_plan": binding}
            ingest(
                session,
                "FREE_CONFIG_CLAIMED",
                user_id=user_id,
                campaign_id=campaign.id,
                idempotency_key=f"claim:{key}",
                metadata={
                    "gift": True,
                    "traffic_bytes": TRAFFIC_BYTES,
                    "validity_hours": 24,
                    "status": "reserved",
                },
            )
        elif (
            allocation.bucket != "gift"
            or allocation.day != day
            or allocation.user_id != user_id
            or allocation.traffic_bytes != TRAFFIC_BYTES
            or allocation.campaign_id != campaign.id
        ):
            return "already_reserved"
        session.commit()  # exact budget and lifetime claim survive every subsequent external effect
    except ValidationError:
        return "unavailable"
    # Retake the person lock after the durable reservation; all duplicate callbacks serialize here.
    _locked_user(session, user_id)
    allocation = session.scalar(
        select(FreeAllocation)
        .where(FreeAllocation.id == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if allocation.status in TERMINAL:
        return "already_reserved"
    claim = session.get(ExclusiveClaim, allocation.claim_id)
    if (
        claim is None
        or claim.claim_key != key
        or claim.traffic_bytes != TRAFFIC_BYTES
        or claim.validity_hours != 24
    ):
        return "unavailable"
    try:
        _campaign(session, flags, day)
        binding = require_tunnel_plan(session, settings)
        _require_audience(session, settings, telegram, user_id)
        if allocation.day != _today():
            allocation.status = "stale"
            claim.status = "stale"
            session.commit()
            return "unavailable"
        adapter = adapter or get_provisioning(settings)
        check_backend(session, adapter)
        _campaign(session, flags, day)
        _require_audience(session, settings, telegram, user_id)
        service = allocation.payload.get("service")
        if not service:
            if claim.provision_attempts >= 3:
                return "already_reserved"
            if allocation.day != _today():
                allocation.status = "stale"
                claim.status = "stale"
                session.commit()
                return "unavailable"
            claim.provision_attempts += 1
            _campaign(session, flags, day)
            service = adapter.create_temp_service(
                location="auto",
                traffic_gb=0,
                traffic_bytes=TRAFFIC_BYTES,
                validity_hours=24,
                protocol="vless",
                idempotency_key=key,
            )
            allocation.payload = {**allocation.payload, "service": service, "tunnel_plan": binding}
        expires = _panel_proof(adapter, service, allocation, claim)
        allocation.service_ref = service["service_ref"]
        allocation.expires_at = expires
        claim.service_ref = service["service_ref"]
        claim.expires_at = expires
        claim.config_payload = {**claim.config_payload, **service, "panel_proven": True}
        claim.status = "gift_active"
        allocation.payload = {**allocation.payload, "panel_proven": True}
        uri, latency = verified_uri(service, settings)
        _panel_proof(adapter, service, allocation, claim)  # identity, quota and expiry reread AFTER HTTPS
        _campaign(session, flags, day)
        require_tunnel_plan(session, settings)
        user = _require_audience(session, settings, telegram, user_id)
        if allocation.day != _today():
            raise ValidationError("welcome gift day ended")
    except Exception as exc:
        allocation.payload = {**allocation.payload, "last_failure": type(exc).__name__}
        allocation.status = "health_pending" if allocation.payload.get("service") else "pending_provision"
        claim.last_provision_error = type(exc).__name__
        session.commit()
        return "unavailable"
    allocation.status = "message_reserved"
    allocation.payload = {
        **allocation.payload,
        "health_method": "https_via_proxy",
        "checked_at_utc": utcnow().isoformat(),
        "latency_ms": latency,
    }
    session.commit()  # one durable private receipt BEFORE calling Telegram; never resend unknown outcomes
    try:
        _campaign(session, flags, day)
        require_tunnel_plan(session, settings)
        user = _require_audience(session, settings, telegram, user_id)
        _panel_proof(adapter, service, allocation, claim)
        _campaign(session, flags, day)
    except Exception as exc:
        allocation.status = "gate_blocked"
        allocation.payload = {**allocation.payload, "last_failure": type(exc).__name__}
        session.commit()
        return "unavailable"
    text = (
        "🎁 <b>هدیهٔ تست اختصاصی سرویس اصلی PV Network</b>\n"
        "📦 حجم شخصی: ۱۰۰ مگابایت · اعتبار حداکثر ۲۴ ساعت یا پایان حجم\n"
        "✅ اتصال واقعی HTTPS از سرور ما پیش از ارسال بررسی شد.\n\n"
        f"<code>{html.escape(uri)}</code>\n\n{config_footer(owned=True)}"
    )
    keyboard = InlineKeyboard([[{"text": "🛒 خرید و تعرفه‌ها", "url": "https://t.me/pvnetwork_bot"}]])
    try:
        result = telegram.send_message(user.telegram_user_id, text, keyboard)
        if not result.get("message_id"):
            raise ValidationError("welcome gift delivery receipt unavailable")
    except Exception:
        allocation.status = "delivery_unknown"
        session.commit()
        return "unavailable"
    allocation.status = "delivered"
    allocation.payload = {**allocation.payload, "message_id": result["message_id"]}
    claim.status = "active"
    ingest(
        session,
        "TRIAL_CREATED",
        user_id=user_id,
        campaign_id=claim.campaign_id,
        idempotency_key=f"trial:{key}",
        metadata={"gift": True, "traffic_bytes": TRAFFIC_BYTES, "validity_hours": 24, "panel_proven": True},
    )
    session.commit()
    return "delivered"
