"""Ten owned shared clients/day, exact quotas, real health, durable effects."""

from __future__ import annotations

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


def publish_shared_slot(
    session: Session, settings: Settings, flags: FlagService, telegram, adapter, *, day: date, slot: int
) -> PublishedPost | None:
    if (
        not 1 <= slot <= 10
        or day != local_day()
        or str(settings.free_channel_id).lower() in FORBIDDEN_MAIN
        or not settings.free_channel_id
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
    allocation = reserve(session, key=key, day=day, bucket="public", traffic_bytes=GIB)
    if allocation.status in {"delivery_unknown", "published", "expired", "provision_failed"}:
        return None
    session.commit()  # reservation survives ambiguous remote effects
    try:
        check_backend(session, adapter)
        if not allocation.payload.get("service"):
            if day != local_day():
                allocation.status = "stale"
                session.commit()
                return None
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
        allocation.status = "health_pending" if allocation.service_ref else "pending_provision"
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
    post = PublishedPost(dedupe_key=key, channel_id=str(settings.free_channel_id), kind="pv_shared")
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
    try:
        result = telegram.send_channel_post(settings.free_channel_id, body, keyboard)
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
        if (
            post is None
            or not post.message_id
            or post.channel_id != str(settings.free_channel_id)
            or post.channel_id.lower() in FORBIDDEN_MAIN
        ):
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
            + "\n🤖 @pvnetwork_bot\n🎁 تست‌های تازه: @pvnetwork_freeconfig"
        )
        try:
            telegram.edit_message_text(post.channel_id, post.message_id, body)
            allocation.status = "expired"
            edited += 1
        except Exception:
            allocation.status = "expiry_notice_unknown"
        session.commit()
    return edited
