"""Insights normalization and downstream-conversion optimizer tests."""

from __future__ import annotations

import uuid
from datetime import datetime

from pv_growth.attribution.service import attribute
from pv_growth.database.models import (
    Campaign,
    ContentInsight,
    ContentItem,
    ContentPublication,
)
from pv_growth.events.service import get_or_create_user, ingest

NOW = datetime(2026, 10, 5, 18, 0, 0)


def _published(session, *, campaign=None, media_id=None):
    campaign = campaign or Campaign(
        code=f"opt_{uuid.uuid4().hex[:8]}", name="Optimizer", kind="purchase",
        status="active", config={"price": 100},
    )
    if campaign.id is None:
        session.add(campaign)
        session.flush()
    item = ContentItem(
        title="published", kind="purchase_cta", channel="instagram", format="post",
        body="caption", facts={"price": campaign.config["price"]}, creative={},
        status="published", campaign_code=campaign.code,
        dedupe_key=f"published:{uuid.uuid4().hex}", published_at=NOW,
    )
    session.add(item)
    session.flush()
    publication = ContentPublication(
        content_id=item.id, provider="instagram", status="published",
        container_id=f"container-{uuid.uuid4().hex[:8]}",
        media_id=media_id or f"media-{uuid.uuid4().hex[:8]}", published_at=NOW,
    )
    session.add(publication)
    session.flush()
    return item, publication, campaign


class _InsightsClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def media_insights(self, media_id, metrics):
        self.calls.append((media_id, tuple(metrics)))
        value = self.payloads[media_id]
        if isinstance(value, Exception):
            raise value
        return value


def test_sync_insights_accepts_partial_metrics_and_refreshes_idempotently(session):
    from pv_growth.instagram.insights import sync_insights

    _, publication, _ = _published(session, media_id="partial")
    client = _InsightsClient({"partial": {
        "data": [{"name": "reach", "values": [{"value": 120}]}]
    }})

    assert sync_insights(session, client, now=NOW) == 1
    client.payloads["partial"] = {
        "data": [
            {"name": "reach", "values": [{"value": 125}]},
            {"name": "likes", "total_value": {"value": 9}},
        ]
    }
    assert sync_insights(session, client, now=NOW) == 1

    rows = session.query(ContentInsight).filter_by(publication_id=publication.id).all()
    assert len(rows) == 1
    assert rows[0].metrics == {"reach": 125, "likes": 9}


def test_meta_outage_isolated_per_publication(session):
    from pv_growth.core.errors import ExternalServiceError
    from pv_growth.instagram.insights import sync_insights

    _published(session, media_id="offline")
    _, good, _ = _published(session, media_id="online")
    client = _InsightsClient({
        "offline": ExternalServiceError("simulated outage"),
        "online": {"data": [{"name": "reach", "values": [{"value": 22}]}]},
    })

    synced = sync_insights(session, client, now=NOW)

    assert synced >= 1
    assert session.query(ContentInsight).filter_by(publication_id=good.id).count() == 1


def test_score_maps_content_campaign_source_to_bot_trial_and_payment(session):
    from pv_growth.content.optimizer import score_content

    item, publication, campaign = _published(session)
    session.add(ContentInsight(
        publication_id=publication.id, captured_at=NOW,
        metrics={"reach": 400, "likes": 20, "comments": 2, "shares": 3, "saves": 4},
    ))
    user, _ = get_or_create_user(
        session, telegram_user_id=8_000_000 + uuid.uuid4().int % 1_000_000
    )
    attribute(session, user.id, f"social_{campaign.code}")
    ingest(session, "TRIAL_CREATED", user_id=user.id, idempotency_key=f"trial:{uuid.uuid4()}")
    ingest(session, "PAYMENT_SUCCESS", user_id=user.id,
           idempotency_key=f"pay:{uuid.uuid4()}", metadata={"amount_cents": 500})
    session.flush()

    score = score_content(session, item.id)

    assert score.bot_starts == 1
    assert score.trials == 1
    assert score.purchases == 1
    assert score.reach == 400
    assert score.engagement == 29
    assert score.sample_size == 1


def test_one_purchase_outranks_reach_only_content(session):
    from pv_growth.content.optimizer import rank_candidates, score_content

    viral, viral_pub, _ = _published(session)
    session.add(ContentInsight(
        publication_id=viral_pub.id, captured_at=NOW,
        metrics={"reach": 1_000_000, "likes": 50_000},
    ))
    buyer_item, buyer_pub, buyer_campaign = _published(session)
    session.add(ContentInsight(
        publication_id=buyer_pub.id, captured_at=NOW, metrics={"reach": 100, "likes": 2}
    ))
    user, _ = get_or_create_user(
        session, telegram_user_id=9_000_000 + uuid.uuid4().int % 500_000
    )
    attribute(session, user.id, f"social_{buyer_campaign.code}")
    ingest(session, "PAYMENT_SUCCESS", user_id=user.id,
           idempotency_key=f"pay:{uuid.uuid4()}")
    session.flush()

    viral_score = score_content(session, viral.id)
    buyer_score = score_content(session, buyer_item.id)
    ranked = rank_candidates(session, [viral.id, buyer_item.id])

    assert buyer_score.score > viral_score.score
    assert ranked[0].content_id == buyer_item.id


def test_weak_sample_never_declares_winner(session):
    from pv_growth.content.optimizer import rank_candidates

    first, _, _ = _published(session)
    second, _, _ = _published(session)

    ranked = rank_candidates(session, [first.id, second.id])

    assert ranked
    assert all(score.sufficient_sample is False for score in ranked)
    assert all(score.is_winner is False for score in ranked)
    assert all(score.confidence == "low" for score in ranked)


def test_insights_job_registered_and_bounded_scheduler(settings, session, monkeypatch):
    from pv_growth.database.models import Job
    from pv_growth.jobs import runner
    from pv_growth.jobs.scheduler import Scheduler

    runner.register_builtin_handlers()
    assert "instagram.insights_sync" in runner._HANDLERS

    before = {row.id for row in session.query(Job).all()}
    monkeypatch.setattr("pv_growth.jobs.scheduler.time.time", lambda: 2_000_000)
    scheduler = Scheduler(settings)
    scheduler._enqueue_periodic()
    scheduler._enqueue_periodic()
    rows = session.query(Job).filter_by(job_type="instagram.insights_sync").all()
    assert len(rows) == 1
    assert rows[0].idempotency_key.startswith("instagram_insights:")
    for row in session.query(Job).all():
        if row.id not in before:
            session.delete(row)
    session.flush()
