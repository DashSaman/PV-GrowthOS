"""Explainable conversion-first scoring for owned social content."""

from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import (
    AppConfig,
    AttributionTouch,
    ContentInsight,
    ContentItem,
    ContentPublication,
    Event,
    Source,
)


@dataclass(frozen=True)
class GrowthScore:
    content_id: int
    score: float
    reach: int
    engagement: int
    bot_starts: int
    trials: int
    purchases: int
    sample_size: int
    confidence: str
    sufficient_sample: bool
    is_winner: bool = False


def _optimizer_config(session: Session) -> dict:
    row = session.get(AppConfig, "growth_optimizer")
    return dict(row.value or {}) if row else {}


def _latest_metrics(session: Session, content_id: int) -> dict:
    publication = session.execute(
        select(ContentPublication).where(
            ContentPublication.content_id == content_id,
            ContentPublication.provider == "instagram",
        )
    ).scalar_one_or_none()
    if publication is None:
        return {}
    insight = session.execute(
        select(ContentInsight)
        .where(ContentInsight.publication_id == publication.id)
        .order_by(ContentInsight.captured_at.desc(), ContentInsight.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return dict(insight.metrics or {}) if insight else {}


def _attributed_counts(session: Session, content_id: int) -> tuple[int, int, int]:
    source = session.execute(
        select(Source).where(Source.code == f"socialc:{content_id}")
    ).scalar_one_or_none()
    if source is None:
        return 0, 0, 0
    user_ids = set(
        session.execute(
            select(AttributionTouch.user_id).where(AttributionTouch.source_id == source.id).distinct()
        )
        .scalars()
        .all()
    )
    if not user_ids:
        return 0, 0, 0
    bot_users = set(
        session.execute(
            select(Event.user_id)
            .where(
                Event.source_id == source.id,
                Event.event_type == "SOURCE_ATTRIBUTED",
                Event.user_id.isnot(None),
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    trial_users = set(
        session.execute(
            select(Event.user_id)
            .where(
                Event.user_id.in_(user_ids),
                Event.event_type.in_(("TRIAL_CREATED", "TRIAL_CONNECTED")),
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    purchase_users = set(
        session.execute(
            select(Event.user_id)
            .where(Event.user_id.in_(user_ids), Event.event_type == "PAYMENT_SUCCESS")
            .distinct()
        )
        .scalars()
        .all()
    )
    return len(bot_users), len(trial_users), len(purchase_users)


def score_content(session: Session, content_id: int) -> GrowthScore:
    item = session.get(ContentItem, content_id)
    if item is None:
        raise ValidationError("content item not found")
    metrics = _latest_metrics(session, content_id)
    reach = int(metrics.get("reach", 0) or 0)
    engagement = sum(int(metrics.get(key, 0) or 0) for key in ("likes", "comments", "shares", "saves"))
    bot_starts, trials, purchases = _attributed_counts(session, item.id)
    config = _optimizer_config(session)
    weights = dict(config.get("weights") or {})
    purchase_weight = float(weights.get("purchase", 1000))
    trial_weight = float(weights.get("trial", 200))
    bot_weight = float(weights.get("bot_start", 20))
    engagement_weight = float(weights.get("engagement", 0.01))
    reach_weight = float(weights.get("reach", 0.001))
    reach_cap = max(0, int(config.get("reach_cap", 10_000)))
    engagement_cap = max(0, int(config.get("engagement_cap", 10_000)))
    min_sample = max(1, int(config.get("min_sample", 20)))
    sample_size = bot_starts
    sufficient = sample_size >= min_sample
    confidence = "high" if sample_size >= min_sample * 5 else ("medium" if sufficient else "low")
    score = (
        purchases * purchase_weight
        + trials * trial_weight
        + bot_starts * bot_weight
        + min(engagement, engagement_cap) * engagement_weight
        + min(reach, reach_cap) * reach_weight
    )
    return GrowthScore(
        content_id=item.id,
        score=round(score, 4),
        reach=reach,
        engagement=engagement,
        bot_starts=bot_starts,
        trials=trials,
        purchases=purchases,
        sample_size=sample_size,
        confidence=confidence,
        sufficient_sample=sufficient,
    )


def rank_candidates(session: Session, content_ids: list[int]) -> list[GrowthScore]:
    ranked = sorted(
        (score_content(session, content_id) for content_id in content_ids),
        key=lambda result: (result.score, result.purchases, result.trials, result.bot_starts),
        reverse=True,
    )
    if not ranked or not ranked[0].sufficient_sample:
        return ranked
    runner_up = ranked[1].score if len(ranked) > 1 else 0
    # Conservative declaration: require a 15% lead once minimum sample exists.
    if runner_up <= 0 or ranked[0].score >= runner_up * 1.15:
        ranked[0] = replace(ranked[0], is_winner=True)
    return ranked


def persist_performance_metadata(session: Session) -> int:
    items = (
        session.execute(
            select(ContentItem).where(ContentItem.channel == "instagram", ContentItem.status == "published")
        )
        .scalars()
        .all()
    )
    if not items:
        return 0
    scores = rank_candidates(session, [item.id for item in items])
    by_id = {score.content_id: score for score in scores}
    for item in items:
        result = by_id[item.id]
        creative = dict(item.creative or {})
        creative.update(
            {
                "performance_score": result.score,
                "performance_confidence": result.confidence,
                "winner": result.is_winner,
            }
        )
        item.creative = creative
    session.flush()
    return len(items)
