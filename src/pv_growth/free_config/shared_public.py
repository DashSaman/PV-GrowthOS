"""Ten owned shared clients/day, exact quotas, real health, durable effects."""

from __future__ import annotations

import html
from datetime import UTC, date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.core.publication_policy import FORBIDDEN_MAIN
from pv_growth.database.models import AppConfig, FreeAllocation, PublishedPost
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.free_config.budgets import GIB, reserve
from pv_growth.free_config.owned_delivery import require_client_identity, verified_uri
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.sales_copy import config_footer
from pv_growth.jobs.runner import RetryableJobError, handler
from pv_growth.provisioning.guard import check_backend
from pv_growth.provisioning.xui import client_email_for, get_provisioning
from pv_growth.telegram.client import InlineKeyboard, TelegramDeliveryUnknownError, get_telegram

TEHRAN = timezone(timedelta(hours=3, minutes=30))
SHARED_HOURS_UTC = (4, 6, 8, 10, 12, 14, 16, 18, 20, 22)


def growth_policy(session: Session) -> dict:
    row = session.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
    return row.value if row is not None and isinstance(row.value, dict) else {}


def local_day(now: datetime | None = None) -> date:
    now = now or utcnow()
    return now.replace(tzinfo=UTC).astimezone(TEHRAN).date()


FREE_SHARED_CHANNELS = {"-1004310246787", "@pvnetwork_freeconfig", "pvnetwork_freeconfig"}


def direct_shared(session: Session) -> bool:
    policy = growth_policy(session)
    return (
        policy.get("owned_shared_direct_enabled") is True
        and policy.get("owned_main_direct_enabled") is not True
        and str(policy.get("owned_shared_channel", "")).strip().lower() in FREE_SHARED_CHANNELS
    )


def publish_shared_slot(
    session: Session, settings: Settings, flags: FlagService, telegram, adapter, *, day: date, slot: int
) -> PublishedPost | None:
    policy = growth_policy(session)
    if policy.get("operational_pause") is True:
        return None
    direct = direct_shared(session)
    destination = policy.get("owned_shared_channel", settings.free_channel_id)
    if policy.get("owned_main_direct_enabled") is True or (
        policy.get("owned_shared_direct_enabled") is True and not direct
    ):
        return None
    if (
        not 1 <= slot <= 10
        or day != local_day()
        or str(destination).strip().lower() in FORBIDDEN_MAIN
        or not destination
    ):
        return None
    try:
        binding = require_tunnel_plan(session, settings)
    except ValidationError:
        return None
    if not growth_policy(session).get("public_shared_enabled") or not all(
        flags.enabled(key)
        for key in ("FREE_CONFIG_ENABLED", "PUBLIC_CONFIG_ENABLED", "PV_EXCLUSIVE_CONFIG_ENABLED")
    ):
        return None
    key = f"pv_shared:{day.isoformat()}:{slot}"
    if session.scalar(select(PublishedPost).where(PublishedPost.dedupe_key == key)) is not None:
        return None
    old = session.get(FreeAllocation, key, populate_existing=True)
    if (
        policy.get("server_autonomy_enabled") is True
        and old is not None
        and not old.service_ref
        and "provision_attempted" not in old.payload
    ):
        return None  # legacy effect state cannot be proved; never replay or relabel it
    from pv_growth.free_config.shared_capacity import has_capacity

    if not has_capacity(session, policy, adapter, current_key=key):
        return None
    allocation = reserve(session, key=key, day=day, bucket="public", traffic_bytes=GIB)
    session.refresh(allocation)
    if allocation.status in {
        "delivery_unknown",
        "published",
        "expired",
        "provision_failed",
        "provision_unknown",
        "provision_reserved",
    }:
        return None
    allocation.status = "provision_reserved"
    allocation.payload = {"provision_attempted": False, **allocation.payload}
    session.commit()  # atomic ownership before even the read-only preflight; other workers must stop
    try:
        check_backend(session, adapter)
        if not allocation.payload.get("service"):
            if day != local_day():
                allocation.status = "stale"
                session.commit()
                return None
            allocation.status = "provision_reserved"
            allocation.payload = {**allocation.payload, "provision_attempted": True}
            session.commit()  # committed capacity survives an ambiguous remote create
            service = adapter.create_temp_service(
                location="auto",
                traffic_gb=1,
                traffic_bytes=GIB,
                validity_hours=24,
                protocol="vless",
                idempotency_key=key,
            )
            expiry_ms = service.get("expiry_ts_ms")
            now_ms = utcnow().replace(tzinfo=UTC).timestamp() * 1000
            if (
                service.get("service_ref") != client_email_for(key)
                or service.get("traffic_bytes") != GIB
                or type(expiry_ms) not in (int, float)
                or not now_ms < expiry_ms <= now_ms + 24 * 3600 * 1000 + 60_000
                or service.get("enabled") is False
            ):
                raise ValidationError("owned service proof mismatch")
            allocation.service_ref = service["service_ref"]
            allocation.payload = {"service": service, "tunnel_plan": binding}
            allocation.expires_at = datetime.fromtimestamp(expiry_ms / 1000, UTC).replace(tzinfo=None)
            allocation.status = "provisioned"
            session.commit()
        service = allocation.payload["service"]
        if allocation.expires_at and allocation.expires_at <= utcnow():
            allocation.status = "expired"
            session.commit()
            return None
        state = adapter.service_state(allocation.service_ref)
        require_client_identity(service, state)
        if (
            state.get("exists") is not True
            or state.get("enabled") is not True
            or state.get("expired")
            or state.get("quota_exhausted")
            or state.get("traffic_limit_bytes") != GIB
        ):
            raise ValidationError("owned quota not confirmed by panel")
        uri, latency = verified_uri(service, settings)
        state = adapter.service_state(allocation.service_ref)
        require_client_identity(service, state)
        if (
            state.get("exists") is not True
            or state.get("enabled") is not True
            or state.get("expired")
            or state.get("quota_exhausted")
            or state.get("traffic_limit_bytes") != GIB
        ):
            raise ValidationError("owned quota ended during health check")
    except Exception as exc:  # sanitized evidence; never a URI or upstream token
        allocation.payload = {**allocation.payload, "last_failure": type(exc).__name__}
        allocation.status = (
            "health_pending"
            if allocation.service_ref
            else "provision_unknown"
            if allocation.payload.get("provision_attempted")
            else "pending_provision"
        )
        session.commit()
        return None
    checked = utcnow()
    allocation.payload = {
        **allocation.payload,
        "health_method": "https_via_proxy",
        "checked_at_utc": checked.isoformat(),
        "latency_ms": latency,
        "verified_uri": uri,
    }
    current = growth_policy(session)
    if (
        day != local_day()
        or direct != direct_shared(session)
        or current.get("public_shared_enabled") is not True
        or current.get("owned_main_direct_enabled") is True
        or current.get("owned_shared_channel", settings.free_channel_id) != destination
    ):
        return None
    post = PublishedPost(dedupe_key=key, channel_id=str(destination), kind="pv_shared")
    session.add(post)
    allocation.status = "message_reserved"
    session.commit()  # no re-send after an uncertain Telegram outcome
    body = (
        "🎁 <b>تست رایگان سرویس اصلی PV Network</b>\n\n"
        "📦 حجم کل: <b>۱ گیگ مشترک</b> بین استفاده‌کنندگان این کانفیگ\n"
        "⏳ اعتبار: حداکثر <b>۲۴ ساعت</b> یا تا پایان حجم\n"
        "✅ اتصال واقعی HTTPS پیش از انتشار از سرور ما بررسی شد.\n\n"
        "🔐 دریافت با عضویت در کانال رایگان و اصلی، ۴۵ روز بدون خرید و نداشتن سرویس فعال.\n"
        "📥 کانفیگ را از دکمهٔ زیر در ربات دریافت کن.\n\n"
        f"{config_footer(owned=True)}"
    )
    keyboard = InlineKeyboard(
        [
            [
                {
                    "text": "📥 دریافت تست ۱ گیگ مشترک",
                    "url": f"https://t.me/pvgrowthos_bot?start=freecfg_shared_{day.isoformat()}_{slot}",
                }
            ],
            [{"text": "🛒 خرید سرویس شخصی PV", "url": "https://t.me/pvnetwork_bot"}],
            [
                {
                    "text": "🎲 شرکت در قرعه‌کشی تست شخصی",
                    "url": "https://t.me/pvgrowthos_bot?start=freecfg_pv_daily_lottery",
                }
            ],
        ]
    )
    from pv_growth.acquisition.entry import share_button
    from pv_growth.acquisition.retail_offer import guide_button

    keyboard.buttons.append([share_button()])
    keyboard.buttons.append([guide_button("owned")])
    if direct:
        body = (
            "💎 <b>تست کیفیت سرویس اصلی PV Network | VIP</b>\n\n"
            "📦 <b>۱ گیگ مشترک</b> از تونل سرورهای اصلی PV؛ حجم بین کاربران مشترک است.\n"
            "⏳ حداکثر <b>۲۴ ساعت</b> یا پایان حجم.\n"
            "✅ اتصال واقعی HTTPS پیش از انتشار از سرور ما بررسی شد.\n"
            "📋 روی کد زیر بزن و کپی کن:\n\n"
            f"<code>{html.escape(uri)}</code>\n\n"
            "🛒 برای حجم و مدت مستقل، سرویس شخصی اصلی را از @pvnetwork_bot بخر.\n"
            f"{config_footer(owned=True)}"
        )
        keyboard = InlineKeyboard(
            [
                [{"text": "🛒 خرید سرویس شخصی VIP", "url": "https://t.me/pvnetwork_bot"}],
                [guide_button("owned")],
                [
                    {
                        "text": "🎁 هدیهٔ هفتگی ۵۰ تا ۱۰۰ مگ",
                        "url": "https://t.me/pvgrowthos_bot?start=freecfg_weekly_member",
                    }
                ],
                [share_button()],
            ]
        )
    try:
        result = telegram.send_channel_post(destination, body, keyboard)
        if not result.get("message_id"):
            raise TelegramDeliveryUnknownError("publication receipt unavailable")
    except Exception:
        allocation.status = "delivery_unknown"
        session.commit()
        return None
    post.message_id = result["message_id"]
    allocation.status = "published"
    ingest(
        session,
        "FREE_CONFIG_POSTED",
        idempotency_key=f"freepost:{key}",
        metadata={
            "dedupe_key": key,
            "kind": "pv_shared",
            "slot": slot,
            "traffic_bytes": GIB,
            "shared": True,
            "health_method": "https_via_proxy",
            "checked_at_utc": checked.isoformat(),
        },
    )
    session.commit()
    return post


@handler("free_config.shared_publish")
def shared_publish_job(session: Session, settings: Settings, payload: dict) -> None:
    day = date.fromisoformat(payload["day"])
    if day != local_day():
        return  # no historical catch-up burst
    post = publish_shared_slot(
        session,
        settings,
        FlagService(settings),
        get_telegram(settings),
        get_provisioning(settings),
        day=day,
        slot=int(payload["slot"]),
    )
    row = session.get(FreeAllocation, f"pv_shared:{day.isoformat()}:{int(payload['slot'])}")
    if post is None and row is not None and row.status in {"pending_provision", "health_pending"}:
        raise RetryableJobError("shared service not yet verified")


def sweep_shared_posts(session: Session, settings: Settings, telegram, adapter) -> int:
    from pv_growth.provisioning.lifecycle_job import expiry_reason

    if adapter is None or not growth_policy(session).get("public_shared_enabled"):
        return 0
    rows = session.scalars(
        select(FreeAllocation)
        .where(FreeAllocation.bucket == "public", FreeAllocation.status == "published")
        .order_by(FreeAllocation.expires_at)
        .limit(30)
    ).all()
    edited = 0
    for allocation in rows:
        try:
            reason = expiry_reason(adapter.service_state(allocation.service_ref))
        except Exception:  # noqa: S112 — unknown panel state must preserve the post, never invent expiry
            continue
        if reason is None:
            continue
        post = session.scalar(select(PublishedPost).where(PublishedPost.dedupe_key == allocation.id))
        if post is None or not post.message_id or post.channel_id.strip().lower() not in FREE_SHARED_CHANNELS:
            continue
        allocation.status = "expiry_notice_reserved"
        allocation.payload = {**allocation.payload, "end_reason": reason}
        session.commit()
        first_line = {
            "volume": "📦 حجم مشترک این تست رایگان تمام شد.",
            "time": "⌛ اعتبار این تست رایگان پایان یافت.",
        }.get(reason, "⏹ این تست رایگان دیگر فعال نیست.")
        body = (
            first_line
            + "\n\n"
            + config_footer(owned=True)
            + "\n🤖 @pvnetwork_bot\n🎁 کانفیگ‌های وب: @pvnetwork_freeconfig"
        )
        try:
            telegram.edit_message_text(post.channel_id, post.message_id, body)
            allocation.status = "expired"
            edited += 1
        except Exception:
            allocation.status = "expiry_notice_unknown"
        session.commit()
    return edited

