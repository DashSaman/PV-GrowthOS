"""Opt-in member gifts: exact shared 50-GiB pool and rolling seven-day lock."""

import secrets
from datetime import timedelta

from sqlalchemy import select

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import AppConfig, Campaign, ExclusiveClaim, FreeAllocation, User
from pv_growth.database.types import utcnow
from pv_growth.free_config.budgets import CAPS, GIB, MIB, lock_day, reserve, used_bytes
from pv_growth.free_config.exclusive import _campaign_window_ok
from pv_growth.free_config.membership import require_membership
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.shared_public import local_day
from pv_growth.jobs.runner import RetryableJobError, handler
from pv_growth.jobs.service import enqueue

CAMPAIGN_CODE = "pv_weekly_gift"


def enabled(session, flags):
    row = session.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
    return bool(
        row
        and isinstance(row.value, dict)
        and row.value.get("weekly_member_gift_enabled") is True
        and flags.enabled("FREE_CONFIG_ENABLED")
        and flags.enabled("PV_EXCLUSIVE_CONFIG_ENABLED")
    )


def campaign_check(session, flags, code, now):
    campaign = session.scalar(
        select(Campaign).where(Campaign.code == code).execution_options(populate_existing=True)
    )
    if (
        not enabled(session, flags)
        or code != CAMPAIGN_CODE
        or campaign is None
        or campaign.kind != "free_config_exclusive"
        or (campaign.config or {}).get("weekly_member_gift") is not True
        or not _campaign_window_ok(campaign, now)
    ):
        raise ValidationError("هدیهٔ هفتگی فعلاً فعال نیست.")
    return campaign


def member_eligible(session, settings, user_id):
    user = session.get(User, user_id, populate_existing=True)
    if not user or user.is_blocked or not user.telegram_user_id or user.telegram_user_id <= 0:
        return False
    try:
        require_membership(session, settings, user_id)
        return True
    except Exception:
        return False


def reserve_weekly(session, settings, flags, *, user_id, now=None):
    live_clock = now is None
    now = now or utcnow()
    day = local_day(now)
    campaign = campaign_check(session, flags, CAMPAIGN_CODE, now)
    require_tunnel_plan(session, settings)
    # Day/global lock is always acquired before the user lock, matching other grants.
    # The user lock prevents cross-midnight races from granting two weekly accounts.
    lock_day(session, day)
    user = session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    if user is None or user.is_blocked or not user.telegram_user_id:
        raise ValidationError("حساب دریافت هدیه قابل تأیید نیست.")
    require_membership(session, settings, user_id)
    if live_clock and local_day() != day:
        raise ValidationError("روز سهمیه تغییر کرده؛ درخواست را دوباره بررسی کن.")
    previous = session.scalar(
        select(FreeAllocation)
        .where(
            FreeAllocation.user_id == user_id,
            FreeAllocation.bucket == "lottery",
            FreeAllocation.created_at > now - timedelta(days=7),
        )
        .order_by(FreeAllocation.created_at.desc())
        .limit(1)
    )
    if previous is not None:
        return previous, False  # reservations and unknown effects also consume the interval
    remaining = CAPS["lottery"] - used_bytes(session, day, "lottery")
    high = min(100, remaining // MIB)
    if high < 50:
        raise ValidationError("سهمیهٔ هدیهٔ امروز تمام شده؛ برای ادامه از ربات خرید استفاده کن.")
    quota = (50 + secrets.randbelow(high - 49)) * MIB
    key = f"weekly_gift:{user_id}:{day}"
    row = reserve(
        session,
        key=key,
        day=day,
        bucket="lottery",
        traffic_bytes=quota,
        user_id=user_id,
        campaign_id=campaign.id,
    )
    row.created_at = now
    row.payload = {"grant_mode": "weekly_member_gift"}
    claim = ExclusiveClaim(
        claim_key=key,
        campaign_id=campaign.id,
        user_id=user_id,
        claim_date=day,
        traffic_gb=quota // GIB,
        traffic_bytes=quota,
        validity_hours=24,
        status="weekly_pending",
    )
    session.add(claim)
    session.flush()
    row.claim_id = claim.id
    enqueue(
        session,
        "free_config.weekly_gift_deliver",
        {"allocation_id": key},
        idempotency_key=f"weekly-deliver:{key}",
        max_attempts=1,
        priority=60,
    )
    session.commit()  # no second grant or retry after ambiguous external effects
    return row, True


@handler("free_config.weekly_gift_deliver")
def deliver_job(session, settings, payload):
    from pv_growth.core.flags import FlagService
    from pv_growth.free_config.lottery import deliver_winner
    from pv_growth.provisioning.xui import get_provisioning
    from pv_growth.telegram.client import get_telegram

    flags = FlagService(settings)
    if not enabled(session, flags):
        return
    row = session.get(FreeAllocation, payload["allocation_id"])
    if not row or row.payload.get("grant_mode") != "weekly_member_gift":
        return
    delivered = deliver_winner(
        session, settings, flags, get_telegram(settings), get_provisioning(settings), allocation_id=row.id
    )
    if not delivered and row.status in {"pending_provision", "health_pending"}:
        raise RetryableJobError("weekly gift not verified; reservation retained")
