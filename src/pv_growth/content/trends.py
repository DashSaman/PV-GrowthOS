"""Policy-safe trend inputs for social planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.database.models import ContentItem
from pv_growth.database.types import utcnow


@dataclass(frozen=True)
class TrendSuggestion:
    topic: str
    hook: str
    source: str
    observed_at: datetime


class TrendProvider(Protocol):
    def suggestions(self, session: Session, *, limit: int = 5) -> list[TrendSuggestion]: ...


class PerformanceTrendProvider:
    """Reuse themes from our own published content; never scrapes Instagram."""

    def suggestions(self, session: Session, *, limit: int = 5) -> list[TrendSuggestion]:
        rows = (
            session.execute(
                select(ContentItem)
                .where(ContentItem.channel == "instagram", ContentItem.status == "published")
                .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
                .limit(max(limit * 4, limit))
            )
            .scalars()
            .all()
        )
        ranked = sorted(
            rows,
            key=lambda item: float((item.creative or {}).get("performance_score", 0)),
            reverse=True,
        )
        return [
            TrendSuggestion(
                topic=item.kind,
                hook=str((item.creative or {}).get("hook") or item.title),
                source="owned_performance",
                observed_at=item.published_at or utcnow(),
            )
            for item in ranked[:limit]
        ]
