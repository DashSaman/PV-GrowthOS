"""Expiry sweep for exclusive free services.

For every ACTIVE claim past its expires_at, verify the real panel state and
only then emit SERVICE_EXPIRED (evidence-backed, never fabricated). The claim
row moves to `expired`. No event is emitted when the panel still reports the
service alive (clock skew tolerance).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger
from pv_growth.database.models import ExclusiveClaim, Job
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.jobs.runner import RetryableJobError, handler
from pv_growth.provisioning.xui import get_provisioning

log = get_logger("provisioning.sweep")


@handler("provisioning.claim")
def retry_pending_claim(session: Session, settings: Settings, payload: dict) -> None:
    claim_id = payload.get("claim_id")
    claim = session.get(ExclusiveClaim, claim_id) if isinstance(claim_id, int) else None
    if claim is None or claim.status != "pending_provision":
        return
    from pv_growth.free_config.audience import guard_reserved_claim

    if not guard_reserved_claim(session, settings, claim):
        return
    job = session.execute(
        select(Job).where(Job.idempotency_key == f"provision:{claim.claim_key}")
    ).scalar_one_or_none()
    adapter = get_provisioning(settings)
    if adapter is None:
        from pv_growth.provisioning.xui import ProvisioningError

        error = ProvisioningError("provisioning backend not configured")
        claim.provision_attempts += 1
        claim.last_provision_error = str(error)
        succeeded = False
    else:
        from pv_growth.free_config.exclusive import provision_reserved_claim

        succeeded = provision_reserved_claim(session, settings, claim, adapter)
    if succeeded:
        return
    if job is not None and job.attempt >= job.max_attempts:
        claim.status = "provision_failed"
        return
    raise RetryableJobError(claim.last_provision_error or "provisioning failed")


def sweep_expired_claims(session: Session, settings: Settings) -> dict:
    adapter = get_provisioning(settings)
    due = (
        session.execute(
            select(ExclusiveClaim)
            .where(
                ExclusiveClaim.status == "active",
            )
            .order_by(ExclusiveClaim.expires_at, ExclusiveClaim.id)
            .limit(200)
        )
        .scalars()
        .all()
    )

    expired = kept = 0
    for claim in due:
        if adapter is None or not claim.service_ref:
            kept += 1
            continue
        try:
            state = adapter.service_state(claim.service_ref)
        except Exception:
            kept += 1
            continue
        reason = expiry_reason(state)
        if reason is None:
            kept += 1
            continue
        claim.status = "expired"
        claim.config_payload = {**(claim.config_payload or {}), "end_reason": reason}
        ingest(
            session,
            "SERVICE_EXPIRED",
            user_id=claim.user_id,
            campaign_id=claim.campaign_id,
            idempotency_key=f"freecfg_exp:{claim.claim_key}",
            metadata={
                "service_ref": claim.service_ref,
                "traffic_gb": claim.traffic_gb,
                "source": "growthos_free_config",
                "reason": reason,
            },
        )
        from pv_growth.database.models import FreeAllocation
        from pv_growth.jobs.service import enqueue

        allocation = session.scalar(
            select(FreeAllocation).where(
                FreeAllocation.claim_id == claim.id, FreeAllocation.bucket.in_(["lottery", "gift"])
            )
        )
        if allocation is not None and allocation.status == "delivered":
            enqueue(
                session,
                "free_config.trial_end",
                {"claim_id": claim.id},
                idempotency_key=f"trial_end:{claim.claim_key}",
                max_attempts=1,
            )
        expired += 1
    if due:
        log.info("expiry sweep", due=len(due), expired=expired, postponed=kept)
    return {"due": len(due), "expired": expired, "postponed": kept}


def expiry_reason(state: dict) -> str | None:
    if state.get("quota_exhausted") is True:
        return "volume"
    if state.get("expired") is True:
        return "time"
    if state.get("exists") is False or state.get("enabled") is False:
        return "unavailable"
    return None


@handler("free_config.trial_end")
def trial_end_job(session: Session, settings: Settings, payload: dict) -> None:
    from pv_growth.core.flags import FlagService
    from pv_growth.database.models import FreeAllocation, MessageLog, User
    from pv_growth.free_config.audience import decision_for_user
    from pv_growth.free_config.shared_public import growth_policy
    from pv_growth.telegram.client import InlineKeyboard, get_telegram

    claim = session.get(ExclusiveClaim, payload.get("claim_id"))
    if (
        claim is None
        or claim.status != "expired"
        or not FlagService(settings).enabled("PV_EXCLUSIVE_CONFIG_ENABLED")
    ):
        return
    allocation = session.scalar(
        select(FreeAllocation).where(
            FreeAllocation.claim_id == claim.id,
            FreeAllocation.bucket.in_(["lottery", "gift"]),
            FreeAllocation.status == "delivered",
        )
    )
    if allocation is None or not decision_for_user(session, settings, claim.user_id).eligible:
        return
    policy_key = "gift_enabled" if allocation.bucket == "gift" else "lottery_enabled"
    if not growth_policy(session).get(policy_key):
        return
    user = session.scalar(select(User).where(User.id == claim.user_id).with_for_update())
    if (
        user is None
        or user.is_blocked
        or user.telegram_chat_id != user.telegram_user_id
        or not user.telegram_user_id
    ):
        return
    key = f"trial_end:{claim.claim_key}"
    if session.scalar(select(MessageLog).where(MessageLog.dedupe_key == key)) is not None:
        return
    from types import SimpleNamespace

    from pv_growth.jobs.runner import DeferredJobError
    from pv_growth.lifecycle.service import cooldown_until, marketing_cooldown

    if marketing_cooldown(session, user.id, 24):
        until = cooldown_until(
            session,
            SimpleNamespace(
                code="pv_trial_end", conditions={"marketing_cooldown_hours": 24}, cooldown_hours=24
            ),
            user.id,
        )
        if until is not None:
            raise DeferredJobError(until)
        return
    row = MessageLog(
        user_id=user.id,
        template_code="pv_trial_end",
        purpose="pv_trial_end",
        dedupe_key=key,
        status="reserved",
        attempts=1,
    )
    session.add(row)
    session.commit()
    # Read purchase state again immediately before the customer-facing effect.
    if not decision_for_user(session, settings, user.id).eligible:
        row.status = "stopped"
        session.commit()
        return
    reason = (claim.config_payload or {}).get("end_reason")
    opening = {
        "volume": "📦 حجم تست شخصی PV تمام شد.",
        "time": "⌛ اعتبار ۲۴ ساعتهٔ تست PV به پایان رسید.",
    }.get(reason, "⏹ تست شخصی PV دیگر فعال نیست.")
    body = (
        opening + "\n\n💎 اگر کیفیت اتصال روی اینترنتت مناسب بود، برای ادامه پلن شخصی با حجم انتخابی بگیر.\n"
        "📱 برای اینستاگرام و اپ‌های آیفون، شرایط پلن را در ربات ببین.\n"
        "📍 برای اپل‌آیدی و سرویس‌های معاملاتی، کشور و شرایط IP همان پلن را پیش از خرید بررسی کن."
    )
    try:
        result = get_telegram(settings).send_message(
            user.telegram_chat_id,
            body,
            InlineKeyboard([[{"text": "🛒 مشاهده پلن‌ها و خرید PV", "url": "https://t.me/pvnetwork_bot"}]]),
        )
        if not result.get("message_id"):
            raise RuntimeError("receipt unavailable")
        row.status = "sent"
        row.meta = {"message_id": result["message_id"], "reason": reason}
        row.sent_at = utcnow()
    except Exception:
        row.status = "delivery_unknown"
        row.meta = {"reason": reason}
    session.commit()


@handler("provisioning.sweep")
def _sweep_job(session: Session, settings: Settings, payload: dict) -> None:
    sweep_expired_claims(session, settings)
    from pv_growth.free_config.shared_public import growth_policy, sweep_shared_posts
    from pv_growth.telegram.client import get_telegram

    if growth_policy(session).get("public_shared_enabled"):
        sweep_shared_posts(session, settings, get_telegram(settings), get_provisioning(settings))
