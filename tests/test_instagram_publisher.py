"""Instagram publication state-machine acceptance tests."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from pv_growth.content import service as content_service
from pv_growth.core.errors import ExternalServiceError, ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import ContentItem, ContentPublication
from pv_growth.database.types import utcnow
from pv_growth.instagram.client import (
    FakeInstagramTransport,
    InstagramAPIError,
    InstagramClient,
)


def _settings(settings, **updates):
    values = {
        "instagram_account_id": "ig-account",
        "instagram_access_token": "secret-token",
        "instagram_api_version": "v-test",
        "flag_content_engine_enabled": True,
        "flag_instagram_automation_enabled": True,
    }
    values.update(updates)
    return settings.model_copy(update=values)


def _item(session, **updates):
    values = {
        "title": "IG test",
        "kind": "education",
        "channel": "instagram",
        "format": "post",
        "body": "safe caption",
        "facts": {},
        "creative": {"media_url": "https://media.example/test.jpg"},
        "status": "draft",
        "scheduled_at": None,
        "dedupe_key": f"ig-test:{uuid.uuid4().hex}",
    }
    values.update(updates)
    item = ContentItem(**values)
    session.add(item)
    session.flush()
    return item


def _publisher(session, settings, transport=None):
    from pv_growth.instagram.publisher import InstagramPublisher

    configured = _settings(settings)
    transport = transport or FakeInstagramTransport()
    client = InstagramClient(configured, transport)
    return InstagramPublisher(session, client), transport


def test_instagram_publisher_happy_path_persists_remote_ids(session, settings):
    publisher, transport = _publisher(session, settings)
    item = _item(session)
    transport.canned[("POST", "/ig-account/media")] = {"id": "container-1"}
    transport.canned[("GET", "/container-1")] = {"status_code": "FINISHED"}
    transport.canned[("POST", "/ig-account/media_publish")] = {"id": "media-1"}

    result = publisher.publish(item, "rendered caption")

    publication = session.query(ContentPublication).filter_by(content_id=item.id).one()
    assert result.remote_id == "media-1"
    assert publication.container_id == "container-1"
    assert publication.media_id == "media-1"
    assert publication.status == "published"
    assert publication.published_at is not None


def test_instagram_publisher_materializes_planned_media_before_container(session, settings):
    from datetime import UTC, datetime
    from datetime import timedelta as dt_timedelta

    from pv_growth.content.media import RenderedAsset
    from pv_growth.content.media_store import PublicAsset
    from pv_growth.instagram.publisher import InstagramPublisher

    class _Renderer:
        def render(self, item):
            return RenderedAsset(Path("/tmp/planned.jpg"), "image/jpeg", 1080, 1080)

    class _Store:
        def put(self, asset, key):
            return PublicAsset(
                "https://media.example/generated/planned.jpg",
                datetime.now(UTC) + dt_timedelta(hours=48),
            )

    configured = _settings(settings)
    transport = FakeInstagramTransport()
    client = InstagramClient(configured, transport)
    publisher = InstagramPublisher(session, client, renderer=_Renderer(), media_store=_Store())
    item = _item(session, creative={"template": "brand_card"})
    transport.canned[("POST", "/ig-account/media")] = {"id": "rendered-container"}
    transport.canned[("GET", "/rendered-container")] = {"status_code": "FINISHED"}
    transport.canned[("POST", "/ig-account/media_publish")] = {"id": "rendered-media"}

    publisher.publish(item, "rendered caption")

    create = next(call for call in transport.calls if call["path"] == "/ig-account/media")
    assert create["data"]["image_url"] == "https://media.example/generated/planned.jpg"
    assert item.creative["media_url"] == "https://media.example/generated/planned.jpg"


class _PublishTimeoutTransport(FakeInstagramTransport):
    def __init__(self):
        super().__init__()
        self.publish_attempts = 0

    def request(self, method, path, *, params=None, data=None, headers=None):
        if method.upper() == "POST" and path == "/ig-account/media_publish":
            self.publish_attempts += 1
            self.calls.append(
                {"method": "POST", "path": path, "params": params, "data": data, "headers": headers}
            )
            raise InstagramAPIError(status_code=None, retryable=True, category="transport", detail="timeout")
        return super().request(method, path, params=params, data=data, headers=headers)


def test_publish_timeout_is_ambiguous_and_never_blindly_repeated(session, settings):
    transport = _PublishTimeoutTransport()
    publisher, _ = _publisher(session, settings, transport)
    item = _item(session)
    transport.canned[("POST", "/ig-account/media")] = {"id": "container-timeout"}
    transport.canned[("GET", "/container-timeout")] = {"status_code": "FINISHED"}

    with pytest.raises(InstagramAPIError):
        publisher.publish(item, "caption")
    publication = session.query(ContentPublication).filter_by(content_id=item.id).one()
    assert publication.container_id == "container-timeout"
    assert publication.status == "publish_unknown"
    assert publication.attempt == 1

    with pytest.raises(ExternalServiceError, match="ambiguous"):
        publisher.publish(item, "caption")
    assert transport.publish_attempts == 1


def test_reconcile_ambiguous_publication_before_retry(session, settings):
    publisher, transport = _publisher(session, settings)
    item = _item(session)
    publication = ContentPublication(
        content_id=item.id,
        provider="instagram",
        status="publish_unknown",
        container_id="container-known",
        attempt=1,
    )
    session.add(publication)
    session.flush()
    transport.canned[("GET", "/ig-account/media")] = {
        "data": [
            {
                "id": "media-reconciled",
                "caption": f"caption https://t.me/pvnetwork_bot?start=socialc_{item.id}",
                "media_type": "IMAGE",
                "permalink": "https://www.instagram.com/p/reconciled/",
                "timestamp": publication.updated_at.isoformat(),
            }
        ],
    }

    reconciled = publisher.reconcile(publication.id)

    assert reconciled.status == "published"
    assert reconciled.media_id == "media-reconciled"
    assert publisher.publish(item, "caption").remote_id == "media-reconciled"
    assert not any(call["path"] == "/ig-account/media_publish" for call in transport.calls)


@pytest.mark.parametrize(
    "remote_rows",
    [
        [],
        [
            {"id": "media-a", "media_type": "IMAGE"},
            {"id": "media-b", "media_type": "IMAGE"},
        ],
    ],
)
def test_reconcile_zero_or_multiple_matches_stays_unknown(session, settings, remote_rows):
    publisher, transport = _publisher(session, settings)
    item = _item(session)
    publication = ContentPublication(
        content_id=item.id,
        provider="instagram",
        status="publish_unknown",
        container_id=f"container-unknown-{item.id}",
        attempt=1,
    )
    session.add(publication)
    session.flush()
    timestamp = publication.updated_at.isoformat()
    rows = []
    for row in remote_rows:
        rows.append(
            {
                **row,
                "caption": f"caption socialc_{item.id}",
                "permalink": "https://www.instagram.com/p/example/",
                "timestamp": timestamp,
            }
        )
    transport.canned[("GET", "/ig-account/media")] = {"data": rows}

    reconciled = publisher.reconcile(publication.id)

    assert reconciled.status == "publish_unknown"
    assert reconciled.media_id is None
    with pytest.raises(ExternalServiceError, match="ambiguous"):
        publisher.publish(item, "caption")
    assert not any(call["path"] == "/ig-account/media_publish" for call in transport.calls)


def test_reconcile_matching_media_outside_time_window_stays_unknown(session, settings):
    publisher, transport = _publisher(session, settings)
    item = _item(session)
    publication = ContentPublication(
        content_id=item.id,
        provider="instagram",
        status="publish_unknown",
        container_id="container-old-match",
        attempt=1,
    )
    session.add(publication)
    session.flush()
    transport.canned[("GET", "/ig-account/media")] = {
        "data": [
            {
                "id": "media-too-old",
                "caption": f"caption socialc_{item.id}",
                "media_type": "IMAGE",
                "permalink": "https://www.instagram.com/p/old/",
                "timestamp": (publication.updated_at - timedelta(hours=1)).isoformat(),
            }
        ]
    }

    assert publisher.reconcile(publication.id).status == "publish_unknown"
    assert publication.media_id is None


def test_story_reconciliation_remains_unknown_without_owned_media_guess(session, settings):
    publisher, transport = _publisher(session, settings)
    item = _item(session, format="story")
    publication = ContentPublication(
        content_id=item.id,
        provider="instagram",
        status="publish_unknown",
        container_id="container-story-unknown",
        attempt=1,
    )
    session.add(publication)
    session.flush()

    reconciled = publisher.reconcile(publication.id)

    assert reconciled.status == "publish_unknown"
    assert reconciled.media_id is None
    assert transport.calls == []


def test_safe_retryable_create_failure_advances_attempt(session, settings):
    class _CreateFailure(FakeInstagramTransport):
        def request(self, method, path, **kwargs):
            if method.upper() == "POST" and path == "/ig-account/media":
                raise InstagramAPIError(status_code=503, retryable=True, category="upstream", detail="busy")
            return super().request(method, path, **kwargs)

    publisher, _ = _publisher(session, settings, _CreateFailure())
    item = _item(session)

    with pytest.raises(InstagramAPIError):
        publisher.publish(item, "caption")

    publication = session.query(ContentPublication).filter_by(content_id=item.id).one()
    assert publication.status == "retryable_error"
    assert publication.attempt == 1
    assert publication.container_id is None


def test_retryable_create_failure_has_bounded_attempt_budget(session, settings):
    class _AlwaysUnavailable(FakeInstagramTransport):
        def request(self, method, path, **kwargs):
            if method.upper() == "POST" and path == "/ig-account/media":
                raise InstagramAPIError(status_code=503, retryable=True, category="upstream", detail="busy")
            return super().request(method, path, **kwargs)

    publisher, _ = _publisher(session, settings, _AlwaysUnavailable())
    item = _item(session)
    publication = ContentPublication(
        content_id=item.id, provider="instagram", status="retryable_error", attempt=4
    )
    session.add(publication)
    session.flush()

    with pytest.raises(ExternalServiceError, match="retry budget exhausted"):
        publisher.publish(item, "caption")

    assert publication.attempt == 5
    assert publication.status == "failed"


def test_instagram_publisher_rejects_unsupported_format(session, settings):
    publisher, transport = _publisher(session, settings)
    item = _item(session, format="carousel")

    with pytest.raises(ValidationError, match="unsupported Instagram format"):
        publisher.publish(item, "caption")
    assert transport.calls == []


def test_job_does_not_construct_instagram_publisher_when_flag_off(session, settings, monkeypatch):
    from pv_growth.content import jobs as content_jobs

    configured = _settings(settings, flag_instagram_automation_enabled=False)
    captured = {}

    class _Flags:
        def __init__(self, _settings):
            pass

        def enabled(self, key):
            return key == "CONTENT_ENGINE_ENABLED"

    def _capture(_session, _settings, _flags, publishers):
        captured.update(publishers)
        return 0

    monkeypatch.setattr(content_jobs, "FlagService", _Flags)
    monkeypatch.setattr(content_jobs.content_service, "publish_due", _capture)
    content_jobs.run_publish_job(session, configured, {})

    assert "instagram" not in captured


def test_instagram_failure_does_not_block_later_telegram_item(session, settings):
    class _BrokenInstagram:
        def publish(self, item, rendered_body):
            raise InstagramAPIError(
                status_code=503, retryable=True, category="upstream", detail="unavailable"
            )

    class _Telegram:
        def publish(self, item, rendered_body):
            from pv_growth.content.publishers import PublishResult

            return PublishResult(remote_id="42", metadata={})

    due = utcnow() - timedelta(minutes=1)
    instagram = _item(session, body="instagram", status="scheduled", scheduled_at=due)
    telegram = _item(
        session,
        channel="free",
        format="text",
        body="telegram",
        status="scheduled",
        scheduled_at=due,
    )
    flags = FlagService(_settings(settings))

    published = content_service.publish_due(
        session, settings, flags, {"instagram": _BrokenInstagram(), "free": _Telegram()}
    )

    assert published == 1
    assert instagram.status == "scheduled"  # transient failure is retried autonomously
    assert telegram.status == "published"
