"""Deterministic social planner driven only by validated runtime data."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.content import service as content_service
from pv_growth.content.trends import PerformanceTrendProvider, TrendProvider, TrendSuggestion
from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger
from pv_growth.database.models import AppConfig, Campaign, ContentItem
from pv_growth.database.types import utcnow

log = get_logger("content.planner")

_DEFAULT_FORMATS = ("reel", "post", "story")
_DEFAULT_SLOTS_UTC = (9, 14, 19)
_DEFAULT_HOOKS = (
    "یک انتخاب ساده برای اتصال روزمره",
    "قبل از انتخاب سرویس این نکته را ببین",
    "اتصال ساده، با اطلاعات شفاف",
)
_FACT_LABELS = {
    "price": "قیمت",
    "traffic_gb": "حجم (GB)",
    "validity_hours": "اعتبار (ساعت)",
    "discount_percent": "تخفیف (%)",
    "max_claims": "ظرفیت",
    "locations": "لوکیشن‌ها",
    "uptime_percent": "آپ‌تایم (%)",
}


def _planner_config(session: Session) -> dict:
    row = session.get(AppConfig, "content_planner")
    return dict(row.value or {}) if row else {}


def _campaigns(session: Session, now: datetime) -> list[Campaign]:
    rows = session.execute(
        select(Campaign).where(Campaign.status == "active").order_by(Campaign.id)
    ).scalars().all()
    return [
        row for row in rows
        if (row.start_at is None or row.start_at <= now)
        and (row.end_at is None or row.end_at >= now)
    ]


def _facts(campaign: Campaign) -> dict:
    config = dict(campaign.config or {})
    return {key: config[key] for key in _FACT_LABELS if key in config}


def _settings_list(config: dict, key: str, default: tuple) -> list:
    value = config.get(key)
    return list(value) if isinstance(value, list) and value else list(default)


def _suggestions(session: Session, provider: TrendProvider, config: dict) -> list[TrendSuggestion]:
    configured_hooks = config.get("hooks")
    if isinstance(configured_hooks, list) and configured_hooks:
        return [
            TrendSuggestion("configured", str(hook), "planner_config", utcnow())
            for hook in configured_hooks if str(hook).strip()
        ]
    try:
        suggestions = provider.suggestions(session, limit=5)
    except Exception as exc:  # noqa: BLE001 — trend failure must not stop planning
        log.warning("trend provider failed; using evergreen", error=type(exc).__name__)
        suggestions = []
    if suggestions:
        return suggestions
    return [
        TrendSuggestion("evergreen", hook, "evergreen", utcnow()) for hook in _DEFAULT_HOOKS
    ]


def _body(facts: dict, cta: str) -> str:
    lines = []
    for key, label in _FACT_LABELS.items():
        if key in facts:
            lines.append(f"{label}: {{{{{key}}}}}")
    lines.extend(("", cta))
    return "\n".join(lines)


def plan_cycle(session: Session, settings: Settings, *, now: datetime | None = None,
               trend_provider: TrendProvider | None = None) -> list[ContentItem]:
    """Create the next bounded set of validated/scheduled Instagram items."""
    del settings  # planner business inputs come from DB, not environment constants
    now = now or utcnow()
    config = _planner_config(session)
    provider = trend_provider or PerformanceTrendProvider()
    suggestions = _suggestions(session, provider, config)
    formats = [str(value) for value in _settings_list(config, "formats", _DEFAULT_FORMATS)
               if str(value) in _DEFAULT_FORMATS]
    if not formats:
        formats = list(_DEFAULT_FORMATS)
    slots = [int(value) for value in _settings_list(config, "slots_utc", _DEFAULT_SLOTS_UTC)
             if isinstance(value, int) and 0 <= value <= 23]
    if not slots:
        slots = list(_DEFAULT_SLOTS_UTC)
    slots = slots[:6]  # hard bound per campaign/cycle

    planned: list[ContentItem] = []
    for campaign in _campaigns(session, now):
        facts = _facts(campaign)
        if not facts:
            continue
        cta = f"https://t.me/pvnetwork_bot?start=social_{campaign.code}"
        for index, hour in enumerate(slots):
            scheduled = datetime.combine(now.date(), time(hour=hour))
            if scheduled <= now:
                scheduled += timedelta(days=1)
            dedupe_key = f"social:{campaign.code}:{scheduled.date().isoformat()}:{hour:02d}"
            exists = session.execute(
                select(ContentItem.id).where(ContentItem.dedupe_key == dedupe_key)
            ).scalar_one_or_none()
            if exists is not None:
                continue
            suggestion = suggestions[index % len(suggestions)]
            format_name = formats[index % len(formats)]
            item = ContentItem(
                title=suggestion.hook[:255],
                kind="purchase_cta" if campaign.kind == "purchase" else "education",
                channel="instagram",
                format=format_name,
                body=_body(facts, cta),
                facts=facts,
                creative={
                    "template": "brand_card",
                    "hook": suggestion.hook,
                    "trend_topic": suggestion.topic,
                    "trend_source": suggestion.source,
                    "cta": cta,
                },
                status="draft",
                dedupe_key=dedupe_key,
                campaign_code=campaign.code,
            )
            session.add(item)
            session.flush()
            content_service.transition(session, item, "validated")
            content_service.schedule(session, item, scheduled)
            planned.append(item)
    if planned:
        log.info("social content planned", count=len(planned))
    return planned
