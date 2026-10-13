"""Finite, once-per-person owned 5-GiB intro and purchase at 24h and channel at 48h."""

from __future__ import annotations

import html
import re
import time
from datetime import timedelta

from sqlalchemy import func, or_, select

from pv_growth.acquisition.daily import BotFailure, PurchaseBot, private_chat, verified_receipt
from pv_growth.core.flags import FlagService
from pv_growth.database.models import (
    AppConfig,
    Campaign,
    ExclusiveClaim,
    FreeAllocation,
    Job,
    MessageLog,
    User,
)
from pv_growth.database.types import utcnow
from pv_growth.events.service import get_or_create_user
from pv_growth.free_config.audience import AudienceDecision, evaluate_history
from pv_growth.free_config.budgets import GIB, lock_day
from pv_growth.free_config.lottery import _panel_proof
from pv_growth.free_config.owned_delivery import verified_uri
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.sales_copy import config_footer
from pv_growth.free_config.shared_public import local_day
from pv_growth.jobs.runner import DeferredJobError, handler
from pv_growth.jobs.service import enqueue
from pv_growth.messaging.service import record_send
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
from pv_growth.provisioning.guard import check_backend
from pv_growth.provisioning.xui import get_provisioning

POLICY = "pv_intro_policy"
CODE = "pv_intro_5g_20261013"
TRAFFIC = 5 * GIB
TOTAL = 10000 * GIB
_worker_key = None
_worker_bot = None
_worker_adapter = None


def close_worker_resources():
    global _worker_key, _worker_bot, _worker_adapter
    for resource in (_worker_bot, _worker_adapter):
        client = getattr(resource, "client", None) or getattr(resource, "_client", None)
        if client is not None:
            client.close()
    _worker_key = _worker_bot = _worker_adapter = None


def worker_resources(settings):
    global _worker_key, _worker_bot, _worker_adapter
    key = tuple(
        getattr(settings, name)
        for name in (
            "purchase_bot_token",
            "provisioning_base_url",
            "provisioning_token",
            "provisioning_inbound_ids",
            "provisioning_sublink",
            "provisioning_ca_bundle",
            "provisioning_tls_verify",
            "provisioning_timeout_seconds",
        )
    )
    if key != _worker_key:
        close_worker_resources()
        _worker_bot = PurchaseBot(settings.purchase_bot_token)
        _worker_adapter = get_provisioning(settings)
        _worker_key = key
    return _worker_bot, _worker_adapter


def valid_user(user):
    return bool(
        user
        and not user.is_blocked
        and type(user.telegram_user_id) is int
        and 0 < user.telegram_user_id < 2**52
        and user.telegram_chat_id == user.telegram_user_id
    )


def other_message(session, user_id, delivered, allocation_id):
    return (
        session.scalar(
            select(MessageLog.id)
            .where(
                MessageLog.user_id == user_id,
                MessageLog.status.in_(["reserved", "sent", "delivery_unknown"]),
                MessageLog.dedupe_key.not_in(
                    [
                        f"intro-send:{allocation_id}",
                        f"intro-followup-send:{allocation_id}",
                        f"intro-channel-send:{allocation_id}",
                    ]
                ),
                or_(MessageLog.sent_at.is_(None), MessageLog.sent_at > delivered),
            )
            .limit(1)
        )
        is not None
    )


def policy(session, settings):
    row = session.get(AppConfig, POLICY, populate_existing=True)
    cfg = row.value if row and isinstance(row.value, dict) else {}
    flags = FlagService(settings)
    if (
        cfg.get("enabled") is not True
        or cfg.get("campaign_code") != CODE
        or type(cfg.get("total_bytes")) is not int
        or cfg["total_bytes"] != TOTAL
        or type(cfg.get("traffic_bytes")) is not int
        or cfg["traffic_bytes"] != TRAFFIC
        or cfg.get("validity_hours") != 24
        or cfg.get("dormant_days") != 30
        or cfg.get("followup_hours") != 24
        or cfg.get("channel_followup_hours") != 48
        or cfg.get("planning_day") != "2026-10-13"
        or type(cfg.get("batch_cap")) is not int
        or not 1 <= cfg["batch_cap"] <= 20
        or not flags.enabled("FREE_CONFIG_ENABLED")
        or not flags.enabled("PV_EXCLUSIVE_CONFIG_ENABLED")
    ):
        return None
    campaign = session.scalar(
        select(Campaign).where(Campaign.code == CODE).execution_options(populate_existing=True)
    )
    if (
        campaign is None
        or campaign.kind != "acquisition_intro"
        or campaign.status != "active"
        or (campaign.config or {}).get("intro_funnel") is not True
        or (campaign.start_at and utcnow() < campaign.start_at)
        or (campaign.end_at and utcnow() >= campaign.end_at)
    ):
        return None
    return cfg, campaign


def decision(reader, recipient):
    try:
        history = reader.fetch_private_invite_history(recipient, require_home=False)
        return (
            evaluate_history(*history, now=utcnow(), dormant_days=30)
            if history is not None
            else AudienceDecision(False, "history_unavailable")
        )
    except Exception:
        return AudienceDecision(False, "history_unavailable")


def continuous_backlog(session, settings):
    current = policy(session, settings)
    if not current or current[0].get("pace_mode") != "continuous_sequential":
        return False
    if local_day(utcnow()).isoformat() != current[0]["planning_day"]:
        return False
    cohort = session.get(AppConfig, "pv_intro_cohort_20261013", populate_existing=True)
    if not cohort or cohort.value.get("status") != "running":
        return False
    return (
        session.scalar(
            select(Job.id)
            .where(
                Job.job_type == "acquisition.intro_deliver",
                Job.status == "pending",
                Job.scheduled_at <= utcnow(),
                Job.attempt < Job.max_attempts,
                Job.attempt < settings.job_max_attempts_default,
                or_(Job.retry_after.is_(None), Job.retry_after <= utcnow()),
            )
            .limit(1)
        )
        is not None
    )


def failure_hint(exc):
    return {
        "xui transport: ReadTimeout": "upstream_read_timeout",
        "xui transport: ConnectTimeout": "upstream_connect_timeout",
        "panel client lookup unavailable": "client_lookup_unavailable",
        "panel traffic usage unavailable": "traffic_usage_unavailable",
        "idempotent client quota mismatch": "quota_mismatch",
    }.get(str(exc), "unclassified")


class ReceiptMismatch(Exception):
    pass


def eligible(reader, recipient):
    return decision(reader, recipient).eligible


def remaining(session, campaign):
    allocated = (
        session.scalar(
            select(func.coalesce(func.sum(FreeAllocation.traffic_bytes), 0)).where(
                FreeAllocation.campaign_id == campaign.id, FreeAllocation.bucket == "acq_intro"
            )
        )
        or 0
    )
    return max(0, TOTAL - int(allocated))


def reserve_intro(session, settings, *, user_id, reader):
    current = policy(session, settings)
    if current is None:
        return None
    cfg, campaign = current
    if local_day(utcnow()).isoformat() != cfg["planning_day"]:
        return None
    require_tunnel_plan(session, settings)
    lock_day(session, local_day(utcnow()))
    # Existing global quota lock serializes reservations across Tehran days.
    user = session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    if not valid_user(user):
        return None
    if session.scalar(
        select(FreeAllocation.id)
        .where(FreeAllocation.user_id == user_id, FreeAllocation.bucket.in_(["gift", "acq_intro"]))
        .limit(1)
    ):
        return None  # includes old failed/unknown gifts; no fresh campaign can replay one
    if remaining(session, campaign) < TRAFFIC or not eligible(reader, user.telegram_user_id):
        return None
    if policy(session, settings) is None or local_day(utcnow()).isoformat() != cfg["planning_day"]:
        return None
    key = f"pv-intro-once:{user_id}"
    claim = ExclusiveClaim(
        claim_key=key,
        campaign_id=campaign.id,
        user_id=user_id,
        claim_date=local_day(utcnow()),
        traffic_gb=5,
        traffic_bytes=TRAFFIC,
        validity_hours=24,
        status="intro_reserved",
        config_payload={"intro_funnel": True, "protocol": "vless"},
    )
    session.add(claim)
    session.flush()
    row = FreeAllocation(
        id=key,
        day=local_day(utcnow()),
        bucket="acq_intro",
        traffic_bytes=TRAFFIC,
        user_id=user_id,
        campaign_id=campaign.id,
        claim_id=claim.id,
        payload={"grant_mode": "acquisition_intro_5g"},
    )
    session.add(row)
    session.commit()
    return row


def _locked(session, allocation_id):
    row = session.get(FreeAllocation, allocation_id)
    if row is None:
        return None, None
    user = session.scalar(
        select(User).where(User.id == row.user_id).with_for_update().execution_options(populate_existing=True)
    )
    row = session.scalar(
        select(FreeAllocation)
        .where(FreeAllocation.id == allocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return row, user


def _bot_identity(bot, settings):
    identity = bot.call("getMe", {})
    return bool(
        identity
        and identity.get("is_bot") is True
        and identity.get("username") == "pvnetwork_bot"
        and str(identity.get("id")) == settings.purchase_bot_token.split(":", 1)[0]
    )


def _body(uri):
    return (
        "🎁 <b>۵ گیگ تست شخصی سرویس اصلی PV Network | VIP</b>\n\n"
        "📦 حجم مخصوص شما: ۵ گیگ · ⏳ حداکثر ۲۴ ساعت یا پایان حجم\n"
        "⚠️ این اشانتیون فقط ۲۴ ساعت معتبر است و تمدید خودکار ندارد.\n"
        "✅ اتصال واقعی HTTPS پیش از تحویل از سرور ما بررسی شد؛ نتیجهٔ اینترنت‌ها ممکن است متفاوت باشد.\n"
        "📋 کد زیر را کپی و در برنامهٔ اتصال وارد کنید:\n\n"
        f"<code>{html.escape(uri)}</code>\n\n"
        "🛒 اگر برای ادامه به حجم و مدت بیشتر نیاز دارید، سرویس شخصی اصلی را از ربات خرید انتخاب کنید.\n"
        + config_footer(owned=True)
    )


def _send(bot, user, body, markup, guard):
    result = bot.call(
        "sendMessage",
        {"chat_id": user.telegram_user_id, "text": body, "parse_mode": "HTML", "reply_markup": markup},
        guard=guard,
    )
    plain = html.unescape(re.sub(r"<[^>]+>", "", body))
    if not verified_receipt(result, user.telegram_user_id, plain, markup):
        raise ReceiptMismatch("purchase bot receipt mismatch")
    return result


def deliver_intro(session, settings, allocation_id, *, reader=None, bot=None, adapter=None):
    reader = reader or MirzaMySQLReader(settings)
    own_bot = bot is None
    bot = bot or PurchaseBot(settings.purchase_bot_token)
    adapter = adapter or get_provisioning(settings)
    try:
        row, user = _locked(session, allocation_id)
        if not row or row.bucket != "acq_intro" or row.status != "reserved" or not valid_user(user):
            return False
        claim = session.get(ExclusiveClaim, row.claim_id)
        current = policy(session, settings)
        if (
            not current
            or local_day(utcnow()).isoformat() != current[0]["planning_day"]
            or row.campaign_id != current[1].id
            or not claim
            or claim.claim_key != row.id
            or claim.user_id != row.user_id
            or claim.campaign_id != row.campaign_id
            or claim.validity_hours != 24
            or claim.traffic_bytes != TRAFFIC
            or row.traffic_bytes != TRAFFIC
            or row.payload.get("grant_mode") != "acquisition_intro_5g"
            or not eligible(reader, user.telegram_user_id)
        ):
            row.status = "audience_blocked"
            session.commit()
            return False
        if not _bot_identity(bot, settings) or not private_chat(
            bot.call("getChat", {"chat_id": user.telegram_user_id}), user.telegram_user_id
        ):
            row.status = "gate_blocked"
            session.commit()
            return False
        binding = require_tunnel_plan(session, settings)
        check_backend(session, adapter)
        if not eligible(reader, user.telegram_user_id):
            row.status = "audience_blocked"
            session.commit()
            return False
        row.status = "provision_reserved"
        claim.provision_attempts = 1
        session.commit()  # consume reservation before the first possibly ambiguous panel call
        stage = "create"
        try:
            service = adapter.create_temp_service(
                location="auto",
                traffic_gb=5,
                traffic_bytes=TRAFFIC,
                validity_hours=24,
                protocol="vless",
                idempotency_key=row.id,
            )
            stage = "panel_before_probe"
            expires = _panel_proof(adapter, service, row, claim)
            stage = "https_probe"
            uri, latency = verified_uri(service, settings)
            stage = "panel_after_probe"
            _panel_proof(adapter, service, row, claim)
            stage = "binding_recheck"
            require_tunnel_plan(session, settings)
        except Exception as exc:
            row.status = "provision_unknown"
            row.payload = {
                **row.payload,
                "failure_class": type(exc).__name__,
                "failure_stage": stage,
                "failure_hint": failure_hint(exc),
            }
            session.commit()
            return False
        row, user = _locked(session, allocation_id)
        if row.status != "provision_reserved":
            return False
        row.service_ref = service["service_ref"]
        row.expires_at = expires
        row.payload = {
            **row.payload,
            "service": service,
            "tunnel_plan": binding,
            "health_method": "https_via_proxy",
            "checked_at_utc": utcnow().isoformat(),
            "latency_ms": latency,
        }
        row.status = "message_reserved"
        receipt, created = record_send(
            session,
            user_id=user.id,
            template_code="pv_intro_5g",
            purpose="pv_intro_5g",
            dedupe_key=f"intro-send:{row.id}",
        )
        if not created:
            row.status = "delivery_unknown"
            session.commit()
            return False
        receipt.attempts = 1
        session.commit()

        def guard():
            latest = policy(session, settings)
            live_user = session.get(User, user.id, populate_existing=True)
            if (
                not latest
                or local_day(utcnow()).isoformat() != latest[0]["planning_day"]
                or latest[1].id != row.campaign_id
                or not valid_user(live_user)
                or not eligible(reader, user.telegram_user_id)
            ):
                raise ValueError("intro audience no longer verified")
            _panel_proof(adapter, service, row, claim)
            require_tunnel_plan(session, settings)

        markup = {
            "inline_keyboard": [
                [{"text": "🛒 خرید و تعرفه‌های سرویس اصلی", "url": "https://t.me/pvnetwork_bot"}]
            ]
        }
        rejected = False
        try:
            result = _send(bot, user, _body(uri), markup, guard)
        except Exception as exc:
            code = exc.code if isinstance(exc, BotFailure) else None
            rejected = code in (400, 403, 404)
            row.payload = {**row.payload, "send_failure_class": type(exc).__name__, "send_failure_code": code}
            result = None
        if not result:
            row.status = "delivery_rejected" if rejected else "delivery_unknown"
            receipt.status = "failed" if rejected else "delivery_unknown"
            receipt.meta = {
                "failure_class": row.payload.get("send_failure_class"),
                "failure_code": row.payload.get("send_failure_code"),
            }
            session.commit()
            return False
        delivered = utcnow()  # followup is anchored to the confirmed send, never account creation
        row.status = "delivered"
        claim.status = "active"
        claim.service_ref = row.service_ref
        claim.expires_at = expires
        row.payload = {
            **row.payload,
            "message_id": result["message_id"],
            "delivered_at_utc": delivered.isoformat(),
            "followup_status": "pending",
            "channel_followup_status": "pending",
        }
        receipt.status = "sent"
        receipt.sent_at = delivered
        receipt.meta = {
            "allocation_id": row.id,
            "source": "channel:acq_intro_20261013",
            "delivery_kind": "acquisition_intro_5g",
            "message_id": result["message_id"],
        }
        schedule_followups(session, row)

        session.commit()
        return True
    finally:
        if own_bot:
            bot.client.close()


def schedule_followups(session, row):
    """Preserve the old 24h job/key; add only the separately deduped 48h stage."""
    from datetime import datetime

    assert row.bucket == "acq_intro" and row.status == "delivered"
    delivered = datetime.fromisoformat(row.payload["delivered_at_utc"])
    for hours, key in ((24, "intro-followup"), (48, "intro-channel")):
        enqueue(
            session,
            "acquisition.intro_followup",
            {"allocation_id": row.id, "stage": hours},
            scheduled_at=delivered + timedelta(hours=hours),
            idempotency_key=f"{key}:{row.id}",
            max_attempts=1,
        )


def followup(session, settings, allocation_id, *, stage=24, reader=None, bot=None):
    if stage not in (24, 48):
        return False
    field = "followup_status" if stage == 24 else "channel_followup_status"
    reader = reader or MirzaMySQLReader(settings)
    own_bot = bot is None
    bot = bot or PurchaseBot(settings.purchase_bot_token)
    try:
        row, user = _locked(session, allocation_id)
        if (
            not row
            or row.bucket != "acq_intro"
            or row.status != "delivered"
            or row.payload.get(field, "pending") != "pending"
            or not valid_user(user)
        ):
            return False
        from datetime import datetime

        delivered = datetime.fromisoformat(row.payload["delivered_at_utc"])
        due = delivered + timedelta(hours=stage)
        if utcnow() < due:
            return False
        if policy(session, settings) is None:
            return False
        if stage == 48 and row.payload.get("followup_status") != "sent":
            row.payload = {**row.payload, field: "purchase_stage_incomplete"}
            session.commit()
            return False
        if other_message(session, user.id, delivered, row.id):
            row.payload = {**row.payload, field: "other_message_suppressed"}
            session.commit()
            return False
        audience = decision(reader, user.telegram_user_id)
        if not audience.eligible:
            row.payload = {
                **row.payload,
                field: (
                    "stopped_after_purchase" if audience.reason == "recent_purchase" else "history_blocked"
                ),
                f"{field}_block_reason": audience.reason,
            }
            session.commit()
            return False
        if not _bot_identity(bot, settings):
            return False
        purpose = "pv_intro_buy24" if stage == 24 else "pv_intro_channel48"
        send_key = "intro-followup-send" if stage == 24 else "intro-channel-send"
        receipt, created = record_send(
            session,
            user_id=user.id,
            template_code=purpose,
            purpose=purpose,
            dedupe_key=f"{send_key}:{row.id}",
        )
        if not created:
            return False
        receipt.attempts = 1
        row.payload = {**row.payload, field: "message_reserved"}
        session.commit()
        body = (
            "🌐 ادامهٔ اتصال رایگان در کانال PV Network\n\n"
            "اگر هنوز اشتراک شخصی نمی‌خواهید، کانفیگ‌های عمومیِ تست‌شده و تست‌های ۱ گیگ مشترک "
            "سرویس اصلی را در @pvnetwork_freeconfig دنبال کنید.\n"
            "⏳ حجم و اعتبار هر تست محدود است؛ منبع عمومی وب متعلق به PV نیست.\n"
            "🛒 برای حجم و مدت مستقل و ادامهٔ استفاده: @pvnetwork_bot"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🎁 ورود به کانال رایگان", "url": "https://t.me/pvnetwork_freeconfig"}],
                [{"text": "🛒 خرید سرویس شخصی", "url": "https://t.me/pvnetwork_bot"}],
                [
                    {
                        "text": "ℹ️ راهنما و هدیهٔ اعضا",
                        "url": "https://t.me/pvgrowthos_bot?start=channel_acq_intro_20261013",
                    }
                ],
            ]
        }

        if stage == 24:
            body = (
                "⏳ <b>مهلت ۲۴ ساعت اشانتیون ۵ گیگی پایان یافت</b>\n\n"
                "این تست فقط ۲۴ ساعت اعتبار داشت و تمدید خودکار ندارد.\n"
                "💎 برای ادامهٔ استفاده، سرویس شخصی همان تونل اصلی PV Network را انتخاب کنید؛ "
                "حجم و مدت دلخواه، راهنمای اتصال و پشتیبانی از ربات رسمی خرید.\n"
                "🛒 مشاهدهٔ پلن‌ها و خرید: @pvnetwork_bot"
            )
            markup = {
                "inline_keyboard": [[{"text": "🛒 خرید سرویس اصلی PV", "url": "https://t.me/pvnetwork_bot"}]]
            }

        def guard():
            live_user = session.scalar(
                select(User)
                .where(User.id == user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                not valid_user(live_user)
                or policy(session, settings) is None
                or not eligible(reader, user.telegram_user_id)
                or other_message(session, user.id, delivered, row.id)
            ):
                raise ValueError("followup stopped by fresh purchase/history check")

        try:
            result = _send(bot, user, body, markup, guard)
        except Exception:
            result = None
        row.payload = {**row.payload, field: "sent" if result else "delivery_unknown"}
        receipt.status = "sent" if result else "delivery_unknown"
        if result:
            receipt.sent_at = utcnow()
            receipt.meta = {
                "source": "channel:acq_intro_20261013",
                "message_id": result["message_id"],
                "stage_hours": stage,
            }
        session.commit()
        return bool(result)
    finally:
        if own_bot:
            bot.client.close()


@handler("acquisition.intro_deliver")
def deliver_job(session, settings, payload):
    bot, adapter = worker_resources(settings)
    deliver_intro(session, settings, payload["allocation_id"], bot=bot, adapter=adapter)


@handler("acquisition.intro_followup")
def followup_job(session, settings, payload):
    stage = payload.get("stage", 24)  # existing jobs become purchase-only, never the old channel offer
    if stage not in (24, 48):
        return
    row = session.get(FreeAllocation, payload["allocation_id"])
    field = "followup_status" if stage == 24 else "channel_followup_status"
    if row and row.status == "delivered" and row.payload.get(field, "pending") == "pending":
        from datetime import datetime

        due = datetime.fromisoformat(row.payload["delivered_at_utc"]) + timedelta(hours=stage)
        if utcnow() < due:
            raise DeferredJobError(due)
    followup(session, settings, payload["allocation_id"], stage=stage)


@handler("acquisition.intro_plan")
def plan_job(session, settings, payload):
    current = policy(session, settings)
    if current is None:
        return
    cfg, campaign = current
    if local_day(utcnow()).isoformat() != cfg["planning_day"]:
        return
    key = "pv_intro_cohort_20261013"
    cohort = session.scalar(
        select(AppConfig)
        .where(AppConfig.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cohort is None or cohort.value.get("status") != "running":
        return
    minute = utcnow().isoformat()[:16]
    if cohort.value.get("last_plan_minute") == minute:
        return
    pending = (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.job_type == "acquisition.intro_deliver", Job.status.in_(["pending", "running"]))
        )
        or 0
    )
    capacity = min(cfg["batch_cap"], max(0, 20 - pending))
    if not capacity:
        return  # no cursor movement while the delivery queue is backed up
    cohort.value = {**cohort.value, "last_plan_minute": minute}
    session.commit()
    contacts = cohort.value["contacts"]
    cursor = cohort.value.get("cursor", 0)
    reader = MirzaMySQLReader(settings)
    planned = 0
    scanned = 0
    started = time.monotonic()
    # Freeze/progress the authorized existing-mainbot cohort; no unbounded discovery loop.
    while (
        cursor < len(contacts)
        and planned < capacity
        and scanned < 30
        and time.monotonic() - started < 30
        and remaining(session, campaign) >= TRAFFIC
    ):
        recipient = contacts[cursor]
        cursor += 1
        scanned += 1
        cohort.value = {**cohort.value, "cursor": cursor}
        session.commit()  # crashes do not replay the advanced recipient
        user, _ = get_or_create_user(session, telegram_user_id=recipient, telegram_chat_id=recipient)
        row = reserve_intro(session, settings, user_id=user.id, reader=reader)
        if row is not None:
            enqueue(
                session,
                "acquisition.intro_deliver",
                {"allocation_id": row.id},
                idempotency_key=f"intro-deliver:{row.id}",
                max_attempts=1,
                priority=-10,
            )
            session.commit()
            planned += 1
        cohort = session.scalar(
            select(AppConfig)
            .where(AppConfig.key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if cohort.value.get("status") != "running":
            break
        cursor = cohort.value["cursor"]
    if cursor >= len(contacts) or remaining(session, campaign) < TRAFFIC:
        cohort.value = {
            **cohort.value,
            "status": "finished",
            "finish_reason": "cohort_end" if cursor >= len(contacts) else "budget_exhausted",
        }
    session.commit()
