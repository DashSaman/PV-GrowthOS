"""Bounds and retention for owned-media Instagram insight collection."""

from datetime import timedelta

from pv_growth.database.models import ContentItem, ContentPublication
from pv_growth.database.types import utcnow
from pv_growth.instagram.insights import prune_insights, sync_insights


class _InsightsClient:
    def __init__(self):
        self.media_ids = []

    def media_insights(self, media_id, metrics):
        self.media_ids.append(media_id)
        return {"data": [{"name": "reach", "values": [{"value": 10}]}]}


def _publication(session, *, suffix: str, published_at):
    item = ContentItem(
        title=f"item {suffix}",
        body="body",
        channel="instagram",
        status="published",
        dedupe_key=f"insight-item:{suffix}",
        published_at=published_at,
    )
    session.add(item)
    session.flush()
    row = ContentPublication(
        content_id=item.id,
        provider="instagram",
        status="published",
        media_id=f"media-{suffix}",
        published_at=published_at,
    )
    session.add(row)
    session.flush()
    return row


def test_sync_insights_honors_recent_window_and_limit(session):
    now = utcnow()
    _publication(session, suffix="old", published_at=now - timedelta(days=30))
    _publication(session, suffix="recent-1", published_at=now - timedelta(days=1))
    _publication(session, suffix="recent-2", published_at=now - timedelta(days=2))
    _publication(session, suffix="recent-3", published_at=now - timedelta(days=3))
    client = _InsightsClient()

    synced = sync_insights(
        session,
        client,
        now=now,
        lookback_days=7,
        limit=2,
    )

    assert synced == 2
    assert client.media_ids == ["media-recent-1", "media-recent-2"]


def test_insight_retention_prunes_only_old_snapshots(session):
    from pv_growth.database.models import ContentInsight

    now = utcnow()
    publication = _publication(session, suffix="retention", published_at=now)
    session.add_all(
        [
            ContentInsight(
                publication_id=publication.id,
                captured_at=now - timedelta(days=120),
                metrics={"reach": 1},
            ),
            ContentInsight(
                publication_id=publication.id,
                captured_at=now - timedelta(days=5),
                metrics={"reach": 2},
            ),
        ]
    )
    session.flush()

    assert prune_insights(session, before=now - timedelta(days=90)) == 1
    remaining = session.query(ContentInsight).filter_by(publication_id=publication.id).all()
    assert [row.metrics["reach"] for row in remaining] == [2]
