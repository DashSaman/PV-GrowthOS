"""Durable jobs for the public free-config acquisition loop."""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.free_config.pipeline import collect_and_stage, publish_public_slot
from pv_growth.jobs.runner import handler
from pv_growth.telegram.client import get_telegram

log = get_logger("free_config.jobs")


@handler("free_config.collect")
def collect_job(session: Session, settings: Settings, payload: dict) -> None:
    del payload
    flags = FlagService(settings)
    if not (flags.enabled("FREE_CONFIG_ENABLED") and flags.enabled("PUBLIC_CONFIG_ENABLED")):
        return
    timeout = max(5.0, settings.health_check_timeout_seconds * 2)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        collect_and_stage(session, client, settings)


@handler("free_config.publish")
def publish_job(session: Session, settings: Settings, payload: dict) -> None:
    flags = FlagService(settings)
    if not (flags.enabled("FREE_CONFIG_ENABLED") and flags.enabled("PUBLIC_CONFIG_ENABLED")):
        return
    if not settings.telegram_bot_token or not settings.free_channel_id:
        log.warning("free config publish skipped: Telegram channel not configured")
        return
    slot = max(1, int(payload.get("slot", 1)))
    publish_public_slot(session, settings, flags, get_telegram(settings), slot=slot)
