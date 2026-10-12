"""Owner-enabled third-party configs: tested publication and gated private receipts."""

import html
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pv_growth.config_quality import probe
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, ConfigSource, MessageLog, PublishedPost, RawConfig, User
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.free_config.branding import brand_uri
from pv_growth.free_config.gated_delivery import _audience
from pv_growth.free_config.membership import require_membership, valid_context
from pv_growth.free_config.sales_copy import config_footer
from pv_growth.telegram.client import InlineKeyboard


def enabled(session, settings, flags=None):
    policy = session.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
    return bool(
        policy
        and isinstance(policy.value, dict)
        and policy.value.get("community_enabled") is True
        and str(settings.free_channel_id)
        in {"-1004310246787", "@pvnetwork_freeconfig", "pvnetwork_freeconfig"}
        and all(
            (flags or FlagService(settings)).enabled(k)
            for k in ("FREE_CONFIG_ENABLED", "PUBLIC_CONFIG_ENABLED")
        )
    )


def current_config(session, settings, config_id):
    if type(config_id) is not int or not 1 <= config_id <= 2**63 - 1:
        return None
    return session.scalar(
        select(RawConfig)
        .join(ConfigSource, ConfigSource.id == RawConfig.source_id)
        .join(PublishedPost, PublishedPost.config_id == RawConfig.id)
        .where(
            RawConfig.id == config_id,
            RawConfig.valid.is_(True),
            RawConfig.health_ok.is_(True),
            RawConfig.status == "published",
            ConfigSource.is_active.is_(True),
            RawConfig.published_at >= utcnow() - timedelta(hours=12),
            RawConfig.fetched_at >= utcnow() - timedelta(hours=24),
            PublishedPost.kind == "free_public",
            PublishedPost.message_id > 0,
            PublishedPost.channel_id == str(settings.free_channel_id),
        )
        .limit(1)
    )


def latest_context(session, settings):
    if not enabled(session, settings):
        return None
    rows = session.scalars(
        select(RawConfig.id)
        .where(RawConfig.status == "published")
        .order_by(RawConfig.published_at.desc())
        .limit(10)
    )
    return next((f"community:{cid}" for cid in rows if current_config(session, settings, cid)), None)


def deliver(session, settings, flags, telegram, *, user_id, context):
    if (
        not valid_context(context)
        or not context.startswith("community:")
        or not enabled(session, settings, flags)
    ):
        return "unavailable"
    user = session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    require_membership(session, settings, user_id, telegram=telegram)
    _audience(session, settings, user_id)
    key = f"free-receipt:{user_id}:{context}"
    if session.scalar(select(MessageLog.id).where(MessageLog.dedupe_key == key)) is not None:
        return "already_delivered"
    row = current_config(session, settings, int(context.split(":")[1]))
    if row is None:
        return "unavailable"
    uri, identity = row.raw_uri, row.uri_hash
    checked = probe.probe_uri(uri, settings)
    if not checked.ok:
        return "unavailable"
    require_membership(session, settings, user_id, telegram=telegram)
    _audience(session, settings, user_id)
    if not enabled(session, settings, flags) or current_config(session, settings, row.id) is None:
        return "unavailable"
    receipt = MessageLog(
        user_id=user_id,
        template_code="community_receipt",
        purpose="free_config",
        dedupe_key=key,
        status="reserved",
        attempts=1,
        meta={"context": context, "health_method": "https_via_proxy", "latency_ms": checked.latency_ms},
    )
    session.add(receipt)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return "already_delivered"
    try:
        require_membership(session, settings, user_id, telegram=telegram)
        _audience(session, settings, user_id)
        fresh = session.get(RawConfig, row.id, populate_existing=True)
        if (
            not enabled(session, settings, flags)
            or fresh.uri_hash != identity
            or fresh.raw_uri != uri
            or current_config(session, settings, row.id) is None
        ):
            receipt.status = "gate_blocked"
            session.commit()
            return "unavailable"
    except ValidationError:
        receipt.status = "gate_blocked"
        session.commit()
        raise
    body = (
        "🌐 <b>کانفیگ رایگان عمومی از وب</b>\n"
        "این کانفیگ متعلق به PV Network نیست؛ حجم، مدت و IP آن تحت کنترل ما نیست.\n"
        "✅ اتصال HTTPS پیش از ارسال از سرور بررسی شد؛ نتیجه روی اینترنت شما ممکن است متفاوت باشد.\n\n"
        f"<code>{html.escape(brand_uri(uri))}</code>\n\n{config_footer()}"
    )
    try:
        result = telegram.send_message(
            user.telegram_user_id,
            body,
            InlineKeyboard([[{"text": "🛒 خرید سرویس شخصی PV", "url": "https://t.me/pvnetwork_bot"}]]),
        )
        if not result.get("message_id"):
            raise ValidationError("receipt unavailable")
    except Exception:
        receipt.status = "delivery_unknown"
        session.commit()
        return "unavailable"
    receipt.status = "sent"
    receipt.sent_at = utcnow()
    receipt.meta = {**receipt.meta, "message_id": result["message_id"]}
    ingest(
        session,
        "FREE_CONFIG_CLAIMED",
        user_id=user_id,
        idempotency_key=f"community:{key}",
        metadata={"delivery_kind": "community", "owned": False, "health_method": "https_via_proxy"},
    )
    session.commit()
    return "delivered"
