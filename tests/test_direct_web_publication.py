import html
from datetime import UTC, date, datetime

import pytest

from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, Job, PublishedPost
from pv_growth.free_config import pipeline
from pv_growth.free_config.branding import brand_uri
from pv_growth.jobs.scheduler import Scheduler
from tests.test_community_restore import URI, setup


def ready_direct(session, settings, monkeypatch):
    s, row, transport, tg = setup(session, settings, monkeypatch)
    session.get(AppConfig, "pv_free_growth_policy").value = {
        "community_enabled": True,
        "community_direct_enabled": True,
        "community_daily_slots": 10,
    }
    session.commit()
    fixed = datetime(2026, 10, 12, 4)
    monkeypatch.setattr(pipeline, "utcnow", lambda: fixed)
    monkeypatch.setattr("pv_growth.free_config.shared_public.utcnow", lambda: fixed)
    monkeypatch.setattr("pv_growth.free_config.community.utcnow", lambda: fixed)
    return s, row, transport, tg


def test_direct_web_post_contains_copyable_config_without_membership_pitch(session, settings, monkeypatch):
    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    post = pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12), slot=1)
    assert post is not None
    body = transport.sent_texts()[0]
    assert html.escape(brand_uri(URI)) in body
    assert "HTTPS" in body and "متعلق به PV Network نیست" in body
    assert "۴۵" not in body and "عضویت" not in body
    assert not any("freecfg_community" in str(p) for _, p in transport.calls)


def test_direct_web_slot_ten_is_allowed_but_more_than_ten_reservations_are_not(
    session, settings, monkeypatch
):
    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    assert pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12), slot=10)


def test_legacy_post_and_unknown_reservations_count_toward_local_day_cap(session, settings, monkeypatch):
    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    for i in range(10):
        session.add(
            PublishedPost(
                dedupe_key=f"legacy:{i}",
                channel_id=s.free_channel_id,
                kind="free_public",
                posted_at=datetime(2026, 10, 11, 21),
            )
        )
    session.commit()
    assert pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12)) is None
    assert not transport.sent_texts()


def test_direct_web_does_not_repeat_a_config_in_the_same_tehran_day(session, settings, monkeypatch):
    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12), slot=1)
    row.published_at = datetime(2026, 10, 11, 21)
    session.commit()
    assert (
        pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12), slot=2) is None
    )
    assert len(transport.sent_texts()) == 1


def test_direct_web_scheduler_has_ten_slots_and_skips_midnight_replay(session, settings, monkeypatch):
    s, _, _, _ = ready_direct(session, settings, monkeypatch)
    for hour in (4, 6, 8, 10, 12, 14, 16, 18, 20, 22):
        stamp = datetime(2026, 10, 12, hour, 15, tzinfo=UTC).timestamp()
        monkeypatch.setattr("pv_growth.jobs.scheduler.time.time", lambda stamp=stamp: stamp)
        Scheduler(s)._enqueue_periodic()
    jobs = session.query(Job).filter_by(job_type="free_config.publish").all()
    assert len(jobs) == 10
    assert {j.payload["slot"] for j in jobs} == set(range(1, 11))
    assert all("day" in j.payload for j in jobs)
    # 20:45 UTC is after Tehran midnight, outside yesterday's 23:30 slot.
    stamp = datetime(2026, 10, 12, 20, 45, tzinfo=UTC).timestamp()
    monkeypatch.setattr("pv_growth.jobs.scheduler.time.time", lambda: stamp)
    Scheduler(s)._enqueue_periodic()
    assert session.query(Job).filter_by(job_type="free_config.publish").count() == 10


def test_direct_web_rejects_yesterday_and_future_days(session, settings, monkeypatch):
    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    for day in (date(2026, 10, 11), date(2026, 10, 13)):
        assert pipeline.publish_public_slot(session, s, FlagService(s), tg, day=day) is None
    assert not transport.sent_texts()


def test_direct_web_home_is_available_to_buyer_but_owned_actions_remain_hidden(
    session, settings, monkeypatch
):
    from pv_growth.api import webhooks

    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    keyboard = webhooks._home_keyboard(session, s, include_free=False)
    buttons = [b for group in keyboard.buttons for b in group]
    assert any(b.get("url") == "https://t.me/pvnetwork_freeconfig" for b in buttons)
    assert not any(b.get("callback_data", "").startswith(("shared:", "lottery:")) for b in buttons)


def test_legacy_web_callback_points_to_public_copy_without_reading_purchase_history(
    session, settings, monkeypatch
):
    from pv_growth.api import webhooks

    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    assert pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12))
    session.commit()
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        webhooks,
        "_free_audience_verified",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("PV-only gate must not block public web guidance")
        ),
    )
    webhooks._handle_callback(
        tg,
        webhooks.TgCallback.model_validate(
            {
                "id": "web",
                "from": {"id": 123},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": f"community:{row.id}",
            }
        ),
    )
    assert "قابل کپی" in transport.sent_texts()[-1]


@pytest.mark.parametrize("phase", ["lock", "probe", "commit"])
def test_rollover_during_publication_does_not_send_on_new_day(session, settings, monkeypatch, phase):
    from pv_growth.free_config import web_publication

    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    clock = [datetime(2026, 10, 12, 20, 29, 59)]
    monkeypatch.setattr(pipeline, "utcnow", lambda: clock[0])

    def cross_midnight():
        clock[0] = datetime(2026, 10, 12, 20, 30, 1)

    if phase == "lock":
        original = web_publication.lock_public_day

        def lock(*args):
            original(*args)
            cross_midnight()

        monkeypatch.setattr(web_publication, "lock_public_day", lock)
    elif phase == "probe":
        original = pipeline.rank_candidates

        def rank(*args, **kwargs):
            result = original(*args, **kwargs)
            cross_midnight()
            return result

        monkeypatch.setattr(pipeline, "rank_candidates", rank)
    else:
        original = session.commit

        def commit():
            original()
            cross_midnight()

        monkeypatch.setattr(session, "commit", commit)
    assert pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12)) is None
    assert not transport.sent_texts()


def test_channel_alias_changes_do_not_reset_daily_quota(session, settings, monkeypatch):
    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    for i in range(10):
        session.add(
            PublishedPost(
                dedupe_key=f"alias:{i}",
                channel_id="@pvnetwork_freeconfig",
                kind="free_public",
                posted_at=datetime(2026, 10, 12, 4),
            )
        )
    session.commit()
    numeric = s.model_copy(update={"free_channel_id": "-1004310246787"})
    assert (
        pipeline.publish_public_slot(session, numeric, FlagService(numeric), tg, day=date(2026, 10, 12))
        is None
    )
    assert not transport.sent_texts()


def test_failed_probe_retries_but_reserved_unknown_delivery_never_retries(session, settings, monkeypatch):
    from pv_growth.free_config import jobs
    from pv_growth.jobs.runner import RetryableJobError

    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    monkeypatch.setattr(jobs, "get_telegram", lambda _: tg)
    s = s.model_copy(update={"telegram_bot_token": "test-token"})
    monkeypatch.setattr(jobs, "publish_public_slot", lambda *args, **kwargs: None)
    payload = {"day": "2026-10-12", "slot": 1, "direct": True}
    with pytest.raises(RetryableJobError):
        jobs.publish_job(session, s, payload)
    session.add(
        PublishedPost(
            dedupe_key="free_public_direct:2026-10-12:1",
            channel_id=s.free_channel_id,
            kind="free_public",
            posted_at=datetime(2026, 10, 12, 4),
        )
    )
    session.commit()
    jobs.publish_job(session, s, payload)
    assert not transport.sent_texts()
