"""One durable private receipt per user and existing owned shared config."""

import html
from datetime import UTC, date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import AppConfig, FreeAllocation, MessageLog, PublishedPost, User
from pv_growth.database.types import utcnow
from pv_growth.free_config.audience import decision_for_user
from pv_growth.free_config.budgets import GIB
from pv_growth.free_config.membership import require_membership, valid_context
from pv_growth.free_config.owned_delivery import require_client_identity, verified_uri
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.sales_copy import config_footer
from pv_growth.provisioning.xui import get_provisioning
from pv_growth.telegram.client import InlineKeyboard


def _enabled(session, flags) -> bool:
    policy = session.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
    return bool(
        policy
        and isinstance(policy.value, dict)
        and policy.value.get("public_shared_enabled") is True
        and all(
            flags.enabled(key)
            for key in ("FREE_CONFIG_ENABLED", "PUBLIC_CONFIG_ENABLED", "PV_EXCLUSIVE_CONFIG_ENABLED")
        )
    )


def _audience(session, settings, user_id):
    policy = session.get(AppConfig, "free_audience_policy", populate_existing=True)
    if policy is not None:
        if not isinstance(policy.value, dict):
            raise ValidationError("وضعیت شرایط دریافت رایگان تأیید نشد.")
        days = policy.value.get("dormant_days", 45)
        if type(days) is not int or days < 45:
            raise ValidationError("شرایط خرید ۴۵ روز اخیر باید بررسی شود.")
    try:
        decision = decision_for_user(session, settings, user_id)
    except Exception as exc:
        raise ValidationError("وضعیت خرید و سرویس فعال شما تأیید نشد.") from exc
    if not decision.eligible or decision.reason not in {"never_purchased", "dormant"}:
        raise ValidationError("این پیشنهاد فقط برای افراد بدون خرید در ۴۵ روز اخیر و بدون سرویس فعال است.")


def _panel_proof(adapter, allocation):
    if allocation.expires_at is None or allocation.expires_at <= utcnow():
        raise ValidationError("اعتبار این کانفیگ پایان یافته است.")
    service = (allocation.payload or {}).get("service")
    if (
        not isinstance(service, dict)
        or not allocation.payload.get("verified_uri")
        or service.get("service_ref") != allocation.service_ref
        or service.get("traffic_bytes") != GIB
        or allocation.traffic_bytes != GIB
    ):
        raise ValidationError("شواهد سرویس مشترک تأیید نشد.")
    state = adapter.service_state(allocation.service_ref)
    require_client_identity(service, state)
    stamp = state.get("expiry_ts_ms")
    if (
        state.get("exists") is not True
        or state.get("enabled") is not True
        or state.get("expired")
        or state.get("quota_exhausted")
        or state.get("traffic_limit_bytes") != GIB
        or type(state.get("traffic_used_bytes")) is not int
        or not 0 <= state["traffic_used_bytes"] < GIB
        or type(stamp) is not int
        or stamp <= utcnow().replace(tzinfo=UTC).timestamp() * 1000
        or stamp != service.get("expiry_ts_ms")
    ):
        raise ValidationError("حجم یا اعتبار این کانفیگ تأیید نشد.")
    return service


def deliver_config(session, settings, flags, telegram, *, user_id: int, context: str, adapter=None) -> str:
    if not valid_context(context) or not context.startswith("shared:") or not _enabled(session, flags):
        return "unavailable"  # old community links cannot deliver configs from another source
    _, day_text, slot = context.split(":")
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        return "unavailable"
    user = session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    require_membership(session, settings, user_id, telegram=telegram)
    _audience(session, settings, user_id)
    key = f"free-receipt:{user_id}:{context}"
    if session.scalar(select(MessageLog.id).where(MessageLog.dedupe_key == key)) is not None:
        return "already_delivered"
    allocation = session.get(FreeAllocation, f"pv_shared:{day.isoformat()}:{slot}", populate_existing=True)
    post = session.scalar(
        select(PublishedPost).where(
            PublishedPost.dedupe_key == f"pv_shared:{day.isoformat()}:{slot}",
            PublishedPost.channel_id == str(settings.free_channel_id),
            PublishedPost.kind == "pv_shared",
        )
    )
    if (
        allocation is None
        or allocation.bucket != "public"
        or allocation.status != "published"
        or post is None
    ):
        return "unavailable"
    try:
        binding = require_tunnel_plan(session, settings)
        evidence = (allocation.payload or {}).get("tunnel_plan")
        if (
            not isinstance(evidence, dict)
            or evidence.get("panel_id") != 33
            or evidence.get("inbound_ids") != binding.get("inbound_ids")
        ):
            return "unavailable"
        adapter = adapter or get_provisioning(settings)
        service = _panel_proof(adapter, allocation)
        uri, latency = verified_uri(service, settings)
        _panel_proof(adapter, allocation)
    except Exception:
        return "unavailable"
    if not _enabled(session, flags):
        return "unavailable"
    require_membership(session, settings, user_id, telegram=telegram)
    _audience(session, settings, user_id)
    receipt = MessageLog(
        user_id=user_id,
        template_code="free_shared_receipt",
        purpose="free_config",
        dedupe_key=key,
        status="reserved",
        attempts=1,
        meta={"context": context, "health_method": "https_via_proxy", "latency_ms": latency},
    )
    session.add(receipt)
    try:
        session.commit()  # durable before sending; unknown outcomes are never retransmitted
    except IntegrityError:
        session.rollback()
        return "already_delivered"
    try:
        require_membership(session, settings, user_id, telegram=telegram)
        _audience(session, settings, user_id)
        if not _enabled(session, flags):
            receipt.status = "gate_blocked"
            session.commit()
            return "unavailable"
    except ValidationError:
        receipt.status = "gate_blocked"
        session.commit()
        raise
    try:
        _panel_proof(adapter, allocation)
    except Exception:
        receipt.status = "gate_blocked"
        session.commit()
        return "unavailable"
    text = (
        "🎁 <b>تست رایگان سرویس اصلی PV Network</b>\n"
        "📦 حجم کل: ۱ گیگ مشترک بین استفاده‌کنندگان این کانفیگ\n"
        "⏳ اعتبار حداکثر ۲۴ ساعت یا تا پایان حجم\n"
        "✅ اتصال واقعی HTTPS پیش از ارسال بررسی شد.\n\n"
        f"<code>{html.escape(uri)}</code>\n\n{config_footer(owned=True)}"
    )
    keyboard = InlineKeyboard([[{"text": "🛒 خرید و تعرفه‌ها", "url": "https://t.me/pvnetwork_bot"}]])
    try:
        result = telegram.send_message(user.telegram_user_id, text, keyboard)
        if not result.get("message_id"):
            raise ValidationError("private receipt unavailable")
    except Exception:
        receipt.status = "delivery_unknown"
        session.commit()
        return "unavailable"
    receipt.status = "sent"
    receipt.sent_at = utcnow()
    receipt.meta = {**receipt.meta, "message_id": result["message_id"]}
    session.commit()
    return "delivered"
