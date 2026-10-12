"""Durable jobs for the public free-config acquisition loop."""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.free_config.pipeline import collect_and_stage, publish_public_slot
from pv_growth.jobs.runner import RetryableJobError, handler
from pv_growth.telegram.client import get_telegram

log = get_logger("free_config.jobs")


@handler("free_config.collect")
def collect_job(session: Session, settings: Settings, payload: dict) -> None:
    del payload
    flags = FlagService(settings)
    if not (flags.enabled("FREE_CONFIG_ENABLED") and flags.enabled("PUBLIC_CONFIG_ENABLED")):
        return
    from pv_growth.free_config.shared_public import growth_policy

    if settings.env == "production" and not growth_policy(session).get("community_enabled", False):
        return
    timeout = max(5.0, settings.health_check_timeout_seconds * 2)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        collect_and_stage(session, client, settings)


@handler("free_config.publish")
def publish_job(session: Session, settings: Settings, payload: dict) -> None:
    flags = FlagService(settings)
    if not (flags.enabled("FREE_CONFIG_ENABLED") and flags.enabled("PUBLIC_CONFIG_ENABLED")):
        return
    from pv_growth.free_config.shared_public import growth_policy

    if settings.env == "production" and not growth_policy(session).get("community_enabled", False):
        return
    if not settings.telegram_bot_token or not settings.free_channel_id:
        log.warning("free config publish skipped: Telegram channel not configured")
        return
    slot = max(1, int(payload.get("slot", 1)))
    from datetime import date

    from pv_growth.free_config.shared_public import local_day
    from pv_growth.free_config.web_publication import direct_enabled

    if direct_enabled(session):
        if payload.get("direct") is not True:
            return  # never replay a legacy two-slot job under the new ten-slot policy
        try:
            day = date.fromisoformat(payload.get("day", ""))
        except (ValueError, TypeError):
            return
        if day != local_day():
            return
    else:
        if payload.get("direct"):
            return
        day = None
    post = publish_public_slot(session, settings, flags, get_telegram(settings), slot=slot, day=day)
    if post is None and day is not None and direct_enabled(session) and day == local_day():
        from sqlalchemy import select

        from pv_growth.database.models import PublishedPost
        from pv_growth.free_config.web_publication import day_reservations

        reserved = session.scalar(
            select(PublishedPost.id).where(
                PublishedPost.dedupe_key == f"free_public_direct:{day.isoformat()}:{slot}"
            )
        )
        if reserved is None and len(day_reservations(session, settings, day)) < 10:
            raise RetryableJobError("no verified public candidate; bounded retry before any send")
