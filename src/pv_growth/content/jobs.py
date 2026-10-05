"""Durable job handlers for the Content Engine."""

from __future__ import annotations

from sqlalchemy.orm import Session

from pv_growth.content import service as content_service
from pv_growth.content.media import MediaRenderer
from pv_growth.content.media_store import build_media_store
from pv_growth.content.optimizer import persist_performance_metadata
from pv_growth.content.planner import plan_cycle
from pv_growth.content.publishers import TelegramPublisher
from pv_growth.core.config import Settings
from pv_growth.core.errors import NotConfigured
from pv_growth.core.flags import FlagService
from pv_growth.instagram.client import InstagramClient
from pv_growth.instagram.insights import sync_insights
from pv_growth.instagram.publisher import InstagramPublisher
from pv_growth.jobs.runner import handler
from pv_growth.telegram.client import get_telegram


def run_publish_job(session: Session, settings: Settings, payload: dict) -> None:
    """Publish due content without letting missing channel config escape the job."""
    publishers = {}
    if settings.telegram_bot_token:
        telegram = TelegramPublisher(get_telegram(settings), settings)
        if settings.free_channel_id:
            publishers["free"] = telegram
        if settings.official_channel_id:
            publishers["official"] = telegram
    flags = FlagService(settings)
    if flags.enabled("INSTAGRAM_AUTOMATION_ENABLED"):
        try:
            renderer = MediaRenderer("/tmp/pv-growth-media")
            media_store = build_media_store(settings)
            renderer.cleanup(retention_hours=settings.media_retention_hours)
            media_store.cleanup()
            publishers["instagram"] = InstagramPublisher(
                session,
                InstagramClient(settings),
                renderer=renderer,
                media_store=media_store,
            )
        except NotConfigured:
            # Fail closed: an Instagram item will be marked failed by the content
            # state machine instead of escaping the durable job handler.
            pass
    content_service.publish_due(session, settings, flags, publishers)


def run_plan_job(session: Session, settings: Settings, payload: dict) -> int:
    """Plan only while the content and Instagram kill switches are both on."""
    flags = FlagService(settings)
    if not (flags.enabled("CONTENT_ENGINE_ENABLED")
            and flags.enabled("INSTAGRAM_AUTOMATION_ENABLED")):
        return 0
    return len(plan_cycle(session, settings))


def run_insights_job(session: Session, settings: Settings, payload: dict) -> int:
    flags = FlagService(settings)
    if not flags.enabled("INSTAGRAM_AUTOMATION_ENABLED"):
        return 0
    try:
        client = InstagramClient(settings)
    except NotConfigured:
        return 0
    synced = sync_insights(session, client)
    persist_performance_metadata(session)
    return synced


@handler("content.publish_due")
def _publish_due_job(session: Session, settings: Settings, payload: dict) -> None:
    run_publish_job(session, settings, payload)


@handler("content.plan")
def _plan_job(session: Session, settings: Settings, payload: dict) -> None:
    run_plan_job(session, settings, payload)


@handler("instagram.insights_sync")
def _insights_job(session: Session, settings: Settings, payload: dict) -> None:
    run_insights_job(session, settings, payload)
