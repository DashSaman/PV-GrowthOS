"""Bounded day fulfillment on the existing Docker scheduler, no Codex dependency."""

import math
from datetime import UTC, datetime, time

from sqlalchemy import select

from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, Job, PublishedPost
from pv_growth.database.types import utcnow
from pv_growth.free_config.shared_public import SHARED_HOURS_UTC, TEHRAN, growth_policy, local_day
from pv_growth.free_config.web_publication import day_reservations, direct_enabled, lock_key
from pv_growth.jobs.runner import handler


def hours_for(session, day):
    policy = growth_policy(session)
    hours = policy.get("public_hours_utc", list(SHARED_HOURS_UTC))
    effective = policy.get("public_schedule_effective_day", "")
    if isinstance(effective, str) and effective and effective <= day.isoformat():
        hours = policy.get("pending_public_hours_utc", hours)
    if (
        not isinstance(hours, list)
        or not 1 <= len(hours) <= 10
        or any(type(h) is not int or not 0 <= h <= 23 for h in hours)
        or len(set(hours)) != len(hours)
    ):
        return list(SHARED_HOURS_UTC)
    return hours


def due_web_target(session, now):
    day = local_day(now)
    stamps = [
        datetime.combine(day, time((h + 3) % 24, 30), tzinfo=TEHRAN).astimezone(UTC).replace(tzinfo=None)
        for h in hours_for(session, day)
    ]
    target = growth_policy(session).get("community_daily_slots", 10)
    if type(target) is not int or not 1 <= target <= 10:
        return 0
    return math.ceil(target * sum(stamp <= now for stamp in stamps) / len(stamps))


def probe_allowance(session, requested):
    policy = growth_policy(session)
    if policy.get("server_autonomy_enabled") is not True:
        return requested
    maximum = policy.get("community_health_attempts_per_day", 600)
    if type(maximum) is not int or not 1 <= maximum <= 1000:
        return 0
    key = f"web-probe-budget:{local_day(utcnow())}"
    lock_key(session, key)
    row = session.get(AppConfig, key, populate_existing=True)
    if row is None:
        row = AppConfig(key=key, value={"attempts": 0})
        session.add(row)
        session.flush()
    attempts = row.value.get("attempts", 0)
    if type(attempts) is not int or attempts < 0:
        return 0
    allowed = min(requested, max(0, maximum - attempts))
    row.value = {**row.value, "attempts": attempts + allowed}
    session.flush()
    return allowed


def fulfill_web(session, settings, telegram):
    from pv_growth.free_config.pipeline import publish_public_slot

    now = utcnow()
    if growth_policy(session).get("server_autonomy_enabled") is not True or not direct_enabled(session):
        return None
    day = local_day(now)
    if len(day_reservations(session, settings, day)) >= due_web_target(session, now):
        return None
    # Archive old jobs untouched; only a position with no effect reservation
    # may be filled by a fresh independently bounded scheduler job.
    for slot in range(1, 11):
        if (
            session.scalar(
                select(PublishedPost.id).where(PublishedPost.dedupe_key == f"free_public_direct:{day}:{slot}")
            )
            is None
        ):
            return publish_public_slot(
                session,
                settings,
                FlagService(settings),
                telegram,
                day=day,
                slot=slot,
                fulfillment_ceiling=due_web_target(session, now),
            )
    return None


@handler("free_config.fulfill_web")
def fulfill_job(session, settings, payload):
    from pv_growth.telegram.client import get_telegram

    if payload.get("day") != local_day(utcnow()).isoformat():
        return
    fulfill_web(session, settings, get_telegram(settings))
    session.commit()  # retain bounded failed checks; never send an unhealthy sample


def enqueue_native(session, settings, now):
    from pv_growth.jobs import service as jobs

    policy = growth_policy(session)
    if policy.get("server_autonomy_enabled") is not True or policy.get("operational_pause") is True:
        return
    day = local_day(now).isoformat()
    if direct_enabled(session) and due_web_target(session, now) > len(
        day_reservations(session, settings, local_day(now))
    ):
        outstanding = session.scalar(
            select(Job.id)
            .where(
                Job.job_type == "free_config.fulfill_web",
                Job.status.in_(["pending", "running"]),
                Job.payload["day"].as_string() == day,
            )
            .limit(1)
        )
        if outstanding is None:
            bucket = int(now.replace(tzinfo=UTC).timestamp()) // 300
            jobs.enqueue(
                session,
                "free_config.fulfill_web",
                {"day": day},
                idempotency_key=f"web_fulfillment:{day}:{bucket}",
                max_attempts=1,
            )
    local = now.replace(tzinfo=UTC).astimezone(TEHRAN)
    if local.hour == 23 and local.minute >= 55:
        jobs.enqueue(
            session,
            "runtime.daily_report",
            {"day": day},
            idempotency_key=f"native_daily_report:{day}",
            max_attempts=1,
        )
