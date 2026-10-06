"""Phase 5 acceptance: commercial facts cannot be fabricated; content state
machine legal; scheduler idempotent; competitor failures isolated; feedback
consent rules."""

import uuid
from datetime import timedelta

import httpx
import pytest

from pv_growth.competitors.watcher import (
    check_source,
    diff_snapshots,
    extract_fields,
    run_watch,
)
from pv_growth.content import service as content
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import (
    Campaign,
    CompetitorChange,
    CompetitorSource,
    ContentItem,
)
from pv_growth.database.types import utcnow
from pv_growth.events.service import get_or_create_user
from pv_growth.feedback import service as feedback
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def _content(session, **kw):
    defaults = dict(
        title="t",
        body="قیمت {{price}} تومان — حجم {{traffic_gb}} گیگ",
        facts={"price": 99000, "traffic_gb": 10},
        channel="free",
    )
    defaults.update(kw)
    item = ContentItem(**defaults)
    session.add(item)
    session.flush()
    return item


def test_content_cannot_fabricate_facts(session):
    campaign = Campaign(
        code=f"c_{uuid.uuid4().hex[:6]}",
        name="C",
        kind="purchase",
        status="active",
        config={"price": 99000, "traffic_gb": 5},
    )
    session.add(campaign)
    item = _content(
        session, campaign_code=campaign.code, facts={"price": 49000, "traffic_gb": 5}
    )  # price contradicts!
    with pytest.raises(ValidationError):  # acceptance: facts cannot be fabricated
        content.transition(session, item, "validated")


def test_content_forbidden_hype(session):
    item = _content(session, facts={"user_count": 50000})  # fake user counts banned
    with pytest.raises(ValidationError):
        content.transition(session, item, "validated")
    item2 = _content(session, facts={"uptime_percent": 99.99})  # no campaign backing
    with pytest.raises(ValidationError):
        content.transition(session, item2, "validated")


def test_content_state_machine(session):
    item = _content(session, facts={"price": 99000, "traffic_gb": 5})
    content.transition(session, item, "validated")
    content.schedule(session, item, utcnow() - timedelta(minutes=1))
    assert item.status == "scheduled"
    # illegal skip draft→published is impossible
    fresh = _content(session, facts={"price": 1, "traffic_gb": 1})
    with pytest.raises(ValidationError):
        content.transition(session, fresh, "published")


def _telegram_publisher(settings):
    from pv_growth.content.publishers import TelegramPublisher

    configured = settings.model_copy(
        update={"free_channel_id": "@freech", "official_channel_id": "@official"}
    )
    transport = FakeTelegramTransport()
    return TelegramPublisher(TelegramClient(transport, configured), configured), transport


def test_telegram_publisher_returns_remote_id(settings, session):
    from pv_growth.content.publishers import PublishResult

    publisher, transport = _telegram_publisher(settings)
    item = _content(session, channel="free", body="hello")

    result = publisher.publish(item, "rendered hello")

    assert result == PublishResult(remote_id="1", metadata={"channel": "@freech"})
    assert transport.sent_texts() == ["rendered hello"]


def test_publish_due_idempotent(settings, session, monkeypatch):
    flags = FlagService(settings.model_copy(update={"flag_content_engine_enabled": True}))
    publisher, transport = _telegram_publisher(settings)
    item = _content(session, facts={"price": 99001, "traffic_gb": 5})  # unique marker
    content.transition(session, item, "validated")
    content.schedule(session, item, utcnow() - timedelta(minutes=1))

    content.publish_due(session, settings, flags, {"free": publisher})
    content.publish_due(session, settings, flags, {"free": publisher})  # double run
    # this item published exactly once and never twice (idempotent scheduler)
    assert item.status == "published" and item.message_id is not None
    my_sends = [p for m, p in transport.calls if m == "sendMessage" and "99001" in p.get("text", "")]
    assert len(my_sends) == 1


def test_publish_due_unknown_channel_does_not_block_later_item(settings, session):
    flags = FlagService(settings.model_copy(update={"flag_content_engine_enabled": True}))
    publisher, transport = _telegram_publisher(settings)
    unknown = _content(session, channel="unknown", facts={"price": 12001, "traffic_gb": 5})
    valid = _content(session, channel="free", facts={"price": 12002, "traffic_gb": 5})
    for item in (unknown, valid):
        content.transition(session, item, "validated")
        content.schedule(session, item, utcnow() - timedelta(minutes=1))

    published = content.publish_due(session, settings, flags, {"free": publisher})

    assert published == 1
    assert unknown.status == "failed"
    assert valid.status == "published"
    assert len(transport.sent_texts()) == 1


class _ExplodingPublisher:
    def publish(self, item, rendered_body):
        raise RuntimeError("simulated publisher outage")


class _RetryablePublisher:
    class RetryableError(RuntimeError):
        retryable = True

    def publish(self, item, rendered_body):
        raise self.RetryableError("temporary upstream outage")


def test_publish_due_publisher_error_isolated_per_item(settings, session):
    flags = FlagService(settings.model_copy(update={"flag_content_engine_enabled": True}))
    publisher, transport = _telegram_publisher(settings)
    broken = _content(session, channel="broken", facts={"price": 13001, "traffic_gb": 5})
    valid = _content(session, channel="free", facts={"price": 13002, "traffic_gb": 5})
    for item in (broken, valid):
        content.transition(session, item, "validated")
        content.schedule(session, item, utcnow() - timedelta(minutes=1))

    published = content.publish_due(
        session, settings, flags, {"broken": _ExplodingPublisher(), "free": publisher}
    )

    assert published == 1
    assert broken.status == "failed"
    assert valid.status == "published"
    assert len(transport.sent_texts()) == 1


def test_publish_due_reschedules_retryable_failure_without_human(settings, session):
    flags = FlagService(settings.model_copy(update={"flag_content_engine_enabled": True}))
    item = _content(session, channel="retry", facts={"price": 13003, "traffic_gb": 5})
    content.transition(session, item, "validated")
    now = utcnow()
    content.schedule(session, item, now - timedelta(minutes=1))

    published = content.publish_due(session, settings, flags, {"retry": _RetryablePublisher()}, now=now)

    assert published == 0
    assert item.status == "scheduled"
    assert item.scheduled_at > now


# ---------- feedback ----------


def test_feedback_rating_and_consent(session):
    user, _ = get_or_create_user(session, telegram_user_id=9500)
    rating = feedback.submit_rating(session, user_id=user.id, rating=5, comment="عالی بود", window_key="svc1")
    assert rating.rating == 5
    dup = feedback.submit_rating(
        session, user_id=user.id, rating=4, window_key="svc1"
    )  # idempotent per window
    assert dup.id == rating.id and dup.rating == 5

    with pytest.raises(ValidationError):
        feedback.submit_rating(session, user_id=user.id, rating=9, window_key="s2")

    feedback.set_testimonial_consent(session, rating, True)
    assert feedback.route(session, rating) == "testimonial_invite"
    assert len(feedback.publishable_testimonials(session)) >= 1


def test_feedback_low_rating_routes_to_recovery(session):
    user, _ = get_or_create_user(session, telegram_user_id=9501)
    low = feedback.submit_rating(session, user_id=user.id, rating=1, window_key="s3")
    assert feedback.route(session, low) == "recovery_workflow"
    with pytest.raises(ValidationError):
        feedback.set_testimonial_consent(session, low, True)  # no consent for low


# ---------- competitor watch ----------

PAGE_V1 = "<html>Price: <b>$5/mo</b> Trial: 3GB</html>"
PAGE_V2 = "<html>Price: <b>$7/mo</b> Trial: 10GB</html>"


def _comp_source(session, page_holder):
    src = CompetitorSource(
        name=f"rival_{uuid.uuid4().hex[:6]}",
        url="https://rival.example/pricing",
        fields={"price": {"regex": r"Price:\s*<b>\$?(\d+)/mo"}, "trial_gb": {"regex": r"Trial:\s*(\d+)GB"}},
    )
    session.add(src)
    session.flush()
    page_holder["source"] = src
    return src


def test_competitor_extraction_and_diff():
    snap = extract_fields(
        PAGE_V1, {"price": {"regex": r"\$(\d+)/mo"}, "trial_gb": {"regex": r"Trial:\s*(\d+)GB"}}
    )
    assert snap == {"price": "5", "trial_gb": "3"}
    changes = diff_snapshots(snap, {"price": "7", "trial_gb": "10"})
    assert set(changes) == {("price", "5", "7"), ("trial_gb", "3", "10")}


def test_competitor_watch_records_changes_once(session):
    holder = {}
    src = _comp_source(session, holder)
    page = {"text": PAGE_V1}
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page["text"]))
    )

    first = check_source(session, src, client)
    assert first == 2  # price + trial first seen
    assert session.query(CompetitorChange).filter_by(source_id=src.id).count() == 2

    check_source(session, src, client)  # no change -> no new records
    assert session.query(CompetitorChange).filter_by(source_id=src.id).count() == 2

    page["text"] = PAGE_V2  # both fields change
    second = check_source(session, src, client)
    assert second == 2
    rows = {
        (r.field, r.old_value, r.new_value)
        for r in session.query(CompetitorChange).filter_by(source_id=src.id)
    }
    assert ("price", "5", "7") in rows and ("trial_gb", "3", "10") in rows


def test_competitor_failure_isolated(settings, session):
    holder = {}
    src = _comp_source(session, holder)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))  # rival down
    result = check_source(session, src, client)
    assert result == -1  # failure swallowed, isolated

    flags = FlagService(settings.model_copy(update={"flag_competitor_watch_enabled": True}))
    summary = run_watch(session, settings, flags, client)
    assert summary["failures"] >= 1 and summary["changes"] == 0  # acceptance: never affects core


def test_competitor_disabled_by_flag(settings, session):
    flags = FlagService(settings)  # OFF by default
    assert run_watch(session, settings, flags) == {"skipped": "disabled"}
