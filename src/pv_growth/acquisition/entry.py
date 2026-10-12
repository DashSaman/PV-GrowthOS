"""Public invitation links contain neither client secrets nor customer identities.

An entry selects a still-current publication; the existing claim callback
rechecks live panel state, HTTPS connectivity, history and both memberships.
Selection here never creates or delivers a client.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select

from pv_growth.core.flags import FlagService
from pv_growth.database.models import FreeAllocation, PublishedPost
from pv_growth.database.types import utcnow
from pv_growth.free_config.budgets import GIB
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.shared_public import growth_policy
from pv_growth.telegram.client import InlineKeyboard

FREE_CHANNELS = {"-1004310246787", "@pvnetwork_freeconfig"}
ACQUISITION_START = re.compile(r"channel_acq_[A-Za-z0-9_]{1,52}\Z")


def is_acquisition_start(value: str | None) -> bool:
    return bool(value and len(value) <= 64 and ACQUISITION_START.fullmatch(value))


def share_url() -> str:
    return "https://t.me/share/url?" + urlencode(
        {
            "url": "https://t.me/pvgrowthos_bot?start=channel_acq_share",
            "text": (
                "🎁 تست سرویس اصلی PV Network\n"
                "کانال رایگان: @pvnetwork_freeconfig\n"
                "📥 پیشنهادهای موجود و شرایط دریافت را در ربات ببین.\n"
                "🔐 عضویت در هر دو کانال، ۴۵ روز بدون خرید و نداشتن سرویس فعال لازم است.\n"
                "🛒 سرویس شخصی و تعرفه‌ها: @pvnetwork_bot"
            ),
        }
    )


def share_button() -> dict:
    return {"text": "📨 معرفی کانال به دوستان", "url": share_url()}


def sharing_prompt() -> tuple[str, InlineKeyboard]:
    return (
        "📨 اگر این کانال برای دوستانت مفید است، معرفی آن را از دکمهٔ زیر بفرست.\n"
        "برای معرفی همراه با اعتبار، لینک شخصی و شرایط فعلی را از بخش معرفی دوستان "
        "در ربات اصلی بگیر؛ اعتبار طبق قانون همان ربات محاسبه می‌شود.",
        InlineKeyboard(
            [
                [share_button()],
                [{"text": "🤝 معرفی دوستان در ربات اصلی", "url": "https://t.me/pvnetwork_bot"}],
            ]
        ),
    )


def latest_shared_context(session, settings) -> str | None:
    if str(settings.free_channel_id).lower() not in FREE_CHANNELS:
        return None
    flags = FlagService(settings)
    if growth_policy(session).get("public_shared_enabled") is not True or not all(
        flags.enabled(key)
        for key in (
            "FREE_CONFIG_ENABLED",
            "PUBLIC_CONFIG_ENABLED",
            "PV_EXCLUSIVE_CONFIG_ENABLED",
        )
    ):
        return None
    try:
        binding = require_tunnel_plan(session, settings)
    except Exception:
        return None
    now = utcnow()
    rows = session.execute(
        select(FreeAllocation, PublishedPost)
        .join(PublishedPost, PublishedPost.dedupe_key == FreeAllocation.id)
        .where(
            FreeAllocation.bucket == "public",
            FreeAllocation.status == "published",
            FreeAllocation.traffic_bytes == GIB,
            FreeAllocation.expires_at > now,
            PublishedPost.kind == "pv_shared",
            PublishedPost.message_id > 0,
            PublishedPost.channel_id == str(settings.free_channel_id),
        )
        .order_by(FreeAllocation.created_at.desc())
        .limit(30)
    )
    for allocation, _ in rows:
        match = re.fullmatch(r"pv_shared:(\d{4}-\d{2}-\d{2}):([1-9]|10)", allocation.id)
        payload = allocation.payload
        if not match or not isinstance(payload, dict) or not allocation.service_ref:
            continue
        if payload.get("health_method") != "https_via_proxy":
            continue
        try:
            checked = datetime.fromisoformat(payload["checked_at_utc"])
            checked = checked.replace(tzinfo=UTC) if checked.tzinfo is None else checked.astimezone(UTC)
        except (ValueError, TypeError, KeyError):
            continue
        if not now.replace(tzinfo=UTC) - timedelta(hours=24) <= checked <= now.replace(tzinfo=UTC):
            continue
        if settings.env == "production" and payload.get("tunnel_plan") != binding:
            continue
        return f"shared:{match[1]}:{match[2]}"
    return None
