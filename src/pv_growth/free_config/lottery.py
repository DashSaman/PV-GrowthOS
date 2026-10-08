"""Private opt-in lottery: freeze once, reserve bytes, prove and deliver once."""

from __future__ import annotations

import html
import secrets
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import (
    AppConfig,
    Campaign,
    ExclusiveClaim,
    FreeAllocation,
    LotteryDraw,
    LotteryEntry,
    User,
)
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.free_config.audience import decision_for_user
from pv_growth.free_config.budgets import CAPS, GIB, MIB, allocate_quotas, lock_day, reserve, used_bytes
from pv_growth.free_config.exclusive import _acquire_quota_locks, _campaign_window_ok
from pv_growth.free_config.membership import require_membership
from pv_growth.free_config.owned_delivery import require_client_identity, verified_uri
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.sales_copy import config_footer
from pv_growth.jobs.runner import RetryableJobError, handler
from pv_growth.jobs.service import enqueue
from pv_growth.provisioning.guard import check_backend
from pv_growth.provisioning.xui import client_email_for, get_provisioning
from pv_growth.telegram.client import InlineKeyboard, get_telegram

CAMPAIGN_CODE = "pv_daily_lottery"
TEHRAN = timezone(timedelta(hours=3, minutes=30))
TERMINAL = {"message_reserved", "delivery_unknown", "delivered", "audience_blocked", "expired", "stale"}


def _utc(now: datetime) -> datetime:
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


def entry_day(now: datetime | None = None) -> date:
    local = _utc(now or utcnow()).astimezone(TEHRAN)
    return local.date() + timedelta(days=int(local.time() >= time(23)))


def _enabled(session: Session, flags: FlagService) -> bool:
    session.flush()
    policy = session.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
    return bool(
        policy
        and isinstance(policy.value, dict)
        and policy.value.get("lottery_enabled") is True
        and flags.enabled("FREE_CONFIG_ENABLED")
        and flags.enabled("PV_EXCLUSIVE_CONFIG_ENABLED")
    )


def _campaign(session: Session, flags: FlagService, code: str, now: datetime) -> Campaign:
    if not _enabled(session, flags):
        raise ValidationError("lottery disabled")
    campaign = session.scalar(
        select(Campaign).where(Campaign.code == code).execution_options(populate_existing=True)
    )
    if (
        code != CAMPAIGN_CODE
        or campaign is None
        or campaign.kind != "free_config_exclusive"
        or (campaign.config or {}).get("lottery") is not True
        or not _campaign_window_ok(campaign, _utc(now).replace(tzinfo=None))
    ):
        raise ValidationError("lottery campaign not active")
    return campaign


def _already_won(session: Session, campaign_id: int, user_id: int) -> bool:
    # Reservation is the win, even when eligibility changes or delivery fails.
    return (
        session.scalar(
            select(ExclusiveClaim.id)
            .where(
                ExclusiveClaim.campaign_id == campaign_id,
                ExclusiveClaim.user_id == user_id,
            )
            .limit(1)
        )
        is not None
    )


def _eligible(session: Session, settings: Settings, user_id: int) -> bool:
    user = session.get(User, user_id, populate_existing=True)
    if user is None or user.is_blocked or not user.telegram_user_id or user.telegram_user_id <= 0:
        return False
    policy = session.get(AppConfig, "free_audience_policy", populate_existing=True)
    if policy is not None:
        if not isinstance(policy.value, dict):
            return False
        days = policy.value.get("dormant_days", 45)
        if type(days) is not int or days < 45:
            return False
    try:
        require_membership(session, settings, user_id)
        decision = decision_for_user(session, settings, user_id)
        return decision.eligible and decision.reason in {"never_purchased", "dormant"}
    except Exception:  # unknown purchase history never grants a trial
        return False


def enter_lottery(
    session: Session,
    settings: Settings,
    flags: FlagService,
    *,
    campaign_code: str,
    user_id: int,
    now: datetime | None = None,
) -> tuple[LotteryEntry, bool]:
    now = now or utcnow()
    campaign = _campaign(session, flags, campaign_code, now)
    require_tunnel_plan(session, settings)
    day = entry_day(now)
    # All private paths take global/day before campaign/day; reserve uses global/day too.
    lock_day(session, day)
    _acquire_quota_locks(session, campaign.id, day)
    if not _eligible(session, settings, user_id):
        raise ValidationError("وضعیت خرید و سرویس فعال شما برای قرعه‌کشی تأیید نشد.")
    if _already_won(session, campaign.id, user_id):
        raise ValidationError("قبلاً در این کمپین برنده شده‌اید.")
    if (
        session.scalar(
            select(LotteryDraw.id).where(LotteryDraw.campaign_id == campaign.id, LotteryDraw.draw_date == day)
        )
        is not None
    ):
        raise ValidationError("این نوبت قرعه‌کشی بسته شده است.")
    existing = session.scalar(
        select(LotteryEntry).where(
            LotteryEntry.campaign_id == campaign.id,
            LotteryEntry.user_id == user_id,
            LotteryEntry.draw_date == day,
        )
    )
    if existing is not None:
        return existing, False
    entry = LotteryEntry(campaign_id=campaign.id, user_id=user_id, draw_date=day)
    session.add(entry)
    session.flush()
    return entry, True


def freeze_draw(
    session: Session,
    settings: Settings,
    flags: FlagService,
    *,
    campaign_code: str,
    day: date,
    now: datetime | None = None,
) -> LotteryDraw:
    now = now or utcnow()
    campaign = _campaign(session, flags, campaign_code, now)
    require_tunnel_plan(session, settings)
    cutoff = datetime.combine(day, time(23), TEHRAN).astimezone(UTC)
    if _utc(now) < cutoff or _utc(now).astimezone(TEHRAN).date() != day:
        raise ValidationError("lottery draw is not due")
    lock_day(session, day)
    _acquire_quota_locks(session, campaign.id, day)
    existing = session.scalar(
        select(LotteryDraw).where(
            LotteryDraw.campaign_id == campaign.id,
            LotteryDraw.draw_date == day,
        )
    )
    if existing is not None:
        return existing
    pool = select(LotteryEntry).where(
        LotteryEntry.campaign_id == campaign.id,
        LotteryEntry.draw_date == day,
        LotteryEntry.status == "entered",
    )
    count = session.scalar(select(func.count()).select_from(pool.subquery())) or 0
    reserved_count = (
        session.scalar(
            select(func.count())
            .select_from(FreeAllocation)
            .where(
                FreeAllocation.day == day,
                FreeAllocation.bucket == "lottery",
            )
        )
        or 0
    )
    capacity = max(0, 100 - reserved_count)
    # Cryptographic sampling uses bounded memory and <=100 eligibility calls, regardless of pool size.
    offsets = secrets.SystemRandom().sample(range(count), min(count, capacity))
    candidates = [
        session.scalar(pool.order_by(LotteryEntry.id).offset(offset).limit(1)) for offset in offsets
    ]
    selected = [
        entry
        for entry in candidates
        if entry is not None
        and not _already_won(session, campaign.id, entry.user_id)
        and _eligible(session, settings, entry.user_id)
    ]
    if _utc(utcnow()).astimezone(TEHRAN).date() != day:
        raise ValidationError("lottery day ended during eligibility checks")
    quotas = allocate_quotas(len(selected), max(0, CAPS["lottery"] - used_bytes(session, day, "lottery")))
    session.execute(
        update(LotteryEntry)
        .where(
            LotteryEntry.campaign_id == campaign.id,
            LotteryEntry.draw_date == day,
            LotteryEntry.status == "entered",
        )
        .values(status="lost")
    )
    draw = LotteryDraw(
        campaign_id=campaign.id,
        draw_date=day,
        status="frozen",
        meta={"entrants": count, "winners": len(quotas), "traffic_bytes": sum(quotas)},
    )
    session.add(draw)
    for entry, quota in zip(selected, quotas, strict=False):
        key = f"lottery:{campaign.id}:{day.isoformat()}:{entry.user_id}"
        allocation = reserve(
            session,
            key=key,
            day=day,
            bucket="lottery",
            traffic_bytes=quota,
            user_id=entry.user_id,
            campaign_id=campaign.id,
        )
        claim = ExclusiveClaim(
            claim_key=key,
            campaign_id=campaign.id,
            user_id=entry.user_id,
            claim_date=day,
            traffic_gb=quota // GIB,
            traffic_bytes=quota,
            validity_hours=24,
            location="auto",
            status="lottery_pending",
            config_payload={"protocol": "vless", "lottery": True},
        )
        session.add(claim)
        session.flush()
        allocation.claim_id = claim.id
        entry.status = "winner"
        entry.allocation_id = key
        ingest(
            session,
            "FREE_CONFIG_CLAIMED",
            user_id=entry.user_id,
            campaign_id=campaign.id,
            idempotency_key=f"claim:{key}",
            metadata={
                "traffic_bytes": quota,
                "validity_hours": 24,
                "lottery": True,
                "status": "lottery_pending",
            },
        )
        enqueue(
            session,
            "free_config.lottery_deliver",
            {"allocation_id": key},
            idempotency_key=f"lottery-deliver:{key}",
            max_attempts=3,
            priority=60,
        )
    session.commit()  # pool and all reservations survive crashes before external effects
    return draw


def _panel_proof(adapter, service: dict, allocation: FreeAllocation, claim: ExclusiveClaim) -> datetime:
    if (
        service.get("service_ref") != client_email_for(claim.claim_key)
        or service.get("traffic_bytes") != allocation.traffic_bytes
    ):
        raise ValidationError("owned service evidence mismatch")
    state = adapter.service_state(service["service_ref"])
    require_client_identity(service, state)
    stamp = state.get("expiry_ts_ms")
    if (
        state.get("exists") is not True
        or state.get("enabled") is not True
        or state.get("traffic_limit_bytes") != allocation.traffic_bytes
        or type(stamp) is not int
        or stamp <= 0
        or stamp != service.get("expiry_ts_ms")
        or type(state.get("traffic_used_bytes")) is not int
        or not 0 <= state["traffic_used_bytes"] < allocation.traffic_bytes
    ):
        raise ValidationError("panel trial proof unavailable")
    expires = datetime.fromtimestamp(stamp / 1000, UTC).replace(tzinfo=None)
    if not utcnow() < expires <= utcnow() + timedelta(hours=24, seconds=60):
        raise ValidationError("panel trial expiry mismatch")
    return expires


def deliver_winner(
    session: Session, settings: Settings, flags: FlagService, telegram, adapter, *, allocation_id: str
) -> bool:
    allocation = session.scalar(
        select(FreeAllocation)
        .where(FreeAllocation.id == allocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if allocation is None or allocation.bucket != "lottery" or allocation.status in TERMINAL:
        return False
    claim = session.get(ExclusiveClaim, allocation.claim_id)
    campaign = session.get(Campaign, allocation.campaign_id)
    if (
        claim is None
        or campaign is None
        or claim.user_id != allocation.user_id
        or claim.traffic_bytes != allocation.traffic_bytes
    ):
        return False

    def current_day() -> bool:
        if allocation.day == _utc(utcnow()).astimezone(TEHRAN).date():
            return True
        allocation.status = "stale"
        claim.status = "stale"
        session.commit()
        return False

    if not current_day():
        return False
    try:
        _campaign(session, flags, campaign.code, utcnow())
    except ValidationError:
        return False
    if not _eligible(session, settings, claim.user_id):
        allocation.status = "audience_blocked"
        claim.status = "audience_blocked"
        session.commit()
        return False
    try:
        binding = require_tunnel_plan(session, settings)
        check_backend(session, adapter)
        if not _eligible(session, settings, claim.user_id):
            allocation.status = "audience_blocked"
            claim.status = "audience_blocked"
            session.commit()
            return False
        service = allocation.payload.get("service")
        if not service:
            if not current_day():
                return False
            claim.provision_attempts += 1
            service = adapter.create_temp_service(
                location="auto",
                traffic_gb=allocation.traffic_bytes // GIB,
                traffic_bytes=allocation.traffic_bytes,
                validity_hours=24,
                protocol="vless",
                idempotency_key=claim.claim_key,
            )
        expires = _panel_proof(adapter, service, allocation, claim)
        allocation.service_ref = service["service_ref"]
        allocation.expires_at = expires
        allocation.payload = {
            **allocation.payload,
            "service": service,
            "panel_proven": True,
            "tunnel_plan": binding,
        }
        claim.service_ref = service["service_ref"]
        claim.expires_at = expires
        claim.config_payload = {**claim.config_payload, **service, "panel_proven": True}
        claim.status = "lottery_active"
        allocation.status = "provisioned"
        ingest(
            session,
            "TRIAL_CREATED",
            user_id=claim.user_id,
            campaign_id=claim.campaign_id,
            idempotency_key=f"trial:{claim.claim_key}",
            metadata={"traffic_bytes": allocation.traffic_bytes, "validity_hours": 24, "panel_proven": True},
        )
        # Lock stays held through health and receipt, preventing concurrent private sends.
        if not _eligible(session, settings, claim.user_id):
            allocation.status = "audience_blocked"
            claim.status = "audience_blocked"
            session.commit()
            return False
        uri, latency = verified_uri(service, settings)
        _campaign(session, flags, campaign.code, utcnow())
        if not _eligible(session, settings, claim.user_id):
            allocation.status = "audience_blocked"
            claim.status = "audience_blocked"
            session.commit()
            return False
        _panel_proof(adapter, service, allocation, claim)
    except Exception as exc:
        allocation.payload = {**allocation.payload, "last_failure": type(exc).__name__}
        allocation.status = "health_pending" if allocation.service_ref else "pending_provision"
        claim.last_provision_error = type(exc).__name__
        session.commit()
        return False
    user = session.get(User, claim.user_id)
    checked = utcnow()
    allocation.payload = {
        **allocation.payload,
        "health_method": "https_via_proxy",
        "checked_at_utc": checked.isoformat(),
        "latency_ms": latency,
    }
    allocation.status = "message_reserved"
    session.commit()  # private receipt is durable BEFORE the Telegram call; unknown results never retransmit
    text = (
        "🎲 <b>برندهٔ قرعه‌کشی تست اختصاصی PV Network شدید!</b>\n"
        f"📦 حجم: {allocation.traffic_bytes // MIB} مگابایت · اعتبار حداکثر ۲۴ ساعت یا پایان حجم\n"
        "✅ اتصال واقعی HTTPS پیش از ارسال از سرور ما بررسی شد.\n\n"
        f"<code>{html.escape(uri)}</code>\n\n"
        f"{config_footer(owned=True)}"
    )
    keyboard = InlineKeyboard([[{"text": "🛒 خرید و تعرفه‌ها", "url": "https://t.me/pvnetwork_bot"}]])
    try:
        result = telegram.send_message(user.telegram_user_id, text, keyboard)
        if not result.get("message_id"):
            raise ValidationError("private delivery receipt unavailable")
    except Exception:
        allocation.status = "delivery_unknown"
        session.commit()
        return False
    allocation.status = "delivered"
    allocation.payload = {**allocation.payload, "message_id": result["message_id"]}
    claim.status = "active"
    ingest(
        session,
        "TRIAL_DELIVERED",
        user_id=claim.user_id,
        campaign_id=claim.campaign_id,
        idempotency_key=f"delivered:{claim.claim_key}",
        metadata={"delivery_kind": "lottery", "owned": True, "health_method": "https_via_proxy"},
    )
    session.commit()
    return True


@handler("free_config.lottery_draw")
def lottery_draw_job(session: Session, settings: Settings, payload: dict) -> None:
    try:
        freeze_draw(
            session,
            settings,
            FlagService(settings),
            campaign_code=payload["campaign_code"],
            day=date.fromisoformat(payload["day"]),
        )
    except ValidationError:
        return


@handler("free_config.lottery_deliver")
def lottery_deliver_job(session: Session, settings: Settings, payload: dict) -> None:
    # Construct clients only after the gate; disabled jobs have no external effects.
    flags = FlagService(settings)
    if not _enabled(session, flags):
        return
    result = deliver_winner(
        session,
        settings,
        flags,
        get_telegram(settings),
        get_provisioning(settings),
        allocation_id=payload["allocation_id"],
    )
    row = session.get(FreeAllocation, payload["allocation_id"])
    if not result and row is not None and row.status in {"pending_provision", "health_pending"}:
        raise RetryableJobError("lottery service not yet verified")
