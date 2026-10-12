"""Explicit owner policy for direct public web configs, separate from PV grants."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pv_growth.database.models import ProvisioningQuotaLock, PublishedPost
from pv_growth.free_config.shared_public import TEHRAN, growth_policy

WEB_HOURS_UTC = (4, 6, 8, 10, 12, 14, 16, 18, 20, 22)
FREE_CHANNEL_ALIASES = {"-1004310246787", "@pvnetwork_freeconfig", "pvnetwork_freeconfig"}


def direct_enabled(session) -> bool:
    policy = growth_policy(session)
    return (
        policy.get("community_enabled") is True
        and policy.get("community_direct_enabled") is True
        and type(policy.get("community_daily_slots")) is int
        and policy["community_daily_slots"] == 10
    )


def day_window(day: date):
    start = datetime.combine(day, datetime.min.time(), tzinfo=TEHRAN).astimezone(UTC).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def lock_public_day(session, day: date) -> None:
    key = f"community-posts:{day.isoformat()}"
    if session.get(ProvisioningQuotaLock, key) is None:
        try:
            with session.begin_nested():
                session.add(ProvisioningQuotaLock(quota_key=key))
                session.flush()
        except IntegrityError:
            pass
    session.execute(
        select(ProvisioningQuotaLock).where(ProvisioningQuotaLock.quota_key == key).with_for_update()
    ).scalar_one()


def day_reservations(session, settings, day: date):
    start, end = day_window(day)
    channel = str(settings.free_channel_id)
    aliases = FREE_CHANNEL_ALIASES if channel in FREE_CHANNEL_ALIASES else {channel}
    return list(
        session.scalars(
            select(PublishedPost)
            .where(
                PublishedPost.kind == "free_public",
                PublishedPost.channel_id.in_(aliases),
                PublishedPost.posted_at >= start,
                PublishedPost.posted_at < end,
            )
            .order_by(PublishedPost.id)
            .limit(11)
        )
    )
