"""Normalize owned-media Instagram insights into durable snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pv_growth.core.logging import get_logger
from pv_growth.database.models import ContentInsight, ContentPublication
from pv_growth.database.types import utcnow
from pv_growth.instagram.client import InstagramClient

log = get_logger("instagram.insights")

INSIGHT_METRICS = ["reach", "views", "likes", "comments", "shares", "saves"]


def _value(row: dict) -> int | float | None:
    total = row.get("total_value")
    if isinstance(total, dict):
        value = total.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    values = row.get("values")
    if isinstance(values, list) and values:
        value = values[-1].get("value") if isinstance(values[-1], dict) else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    value = row.get("value")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    return None


def normalize_metrics(payload: dict) -> dict:
    normalized: dict[str, int | float] = {}
    data = payload.get("data", [])
    if not isinstance(data, list):
        return normalized
    for row in data:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        value = _value(row)
        if value is not None:
            normalized[str(row["name"])[:64]] = value
    return normalized


def sync_insights(
    session: Session,
    client: InstagramClient,
    *,
    now: datetime | None = None,
    lookback_days: int = 30,
    limit: int = 100,
) -> int:
    captured_at = now or utcnow()
    cutoff = captured_at - timedelta(days=max(1, lookback_days))
    publications = (
        session.execute(
            select(ContentPublication)
            .where(
                ContentPublication.provider == "instagram",
                ContentPublication.status == "published",
                ContentPublication.media_id.isnot(None),
                func.coalesce(
                    ContentPublication.published_at,
                    ContentPublication.created_at,
                )
                >= cutoff,
            )
            .order_by(
                func.coalesce(
                    ContentPublication.published_at,
                    ContentPublication.created_at,
                ).desc(),
                ContentPublication.id.desc(),
            )
            .limit(max(1, limit))
        )
        .scalars()
        .all()
    )
    synced = 0
    for publication in publications:
        try:
            payload = client.media_insights(publication.media_id, INSIGHT_METRICS)
        except Exception as exc:  # noqa: BLE001 — isolate each owned media item
            log.warning(
                "Instagram insight sync failed",
                publication_id=publication.id,
                error=type(exc).__name__,
            )
            continue
        metrics = normalize_metrics(payload)
        snapshot = session.execute(
            select(ContentInsight).where(
                ContentInsight.publication_id == publication.id,
                ContentInsight.captured_at == captured_at,
            )
        ).scalar_one_or_none()
        if snapshot is None:
            snapshot = ContentInsight(
                publication_id=publication.id,
                captured_at=captured_at,
                metrics=metrics,
            )
            session.add(snapshot)
        else:
            snapshot.metrics = metrics
        session.flush()
        synced += 1
    return synced


def prune_insights(session: Session, *, before: datetime) -> int:
    result = session.execute(delete(ContentInsight).where(ContentInsight.captured_at < before))
    return int(result.rowcount or 0)
