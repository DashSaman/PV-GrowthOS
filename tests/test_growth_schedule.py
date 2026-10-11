from datetime import datetime, timedelta

import pytest

from pv_growth.database.models import AppConfig, Job
from pv_growth.jobs.scheduler import enqueue_free_growth


@pytest.fixture
def enabled_growth(session):
    session.add(
        AppConfig(key="pv_free_growth_policy", value={"public_shared_enabled": True, "lottery_enabled": True})
    )
    session.commit()


def test_shared_schedule_has_ten_slots_per_tehran_day(session, settings, enabled_growth):
    # Each Tehran day starts with slot 10 at 01:30 (the preceding UTC day's
    # 22:00), then slots 1-9 at 07:30 through 23:30. Tick through two full
    # local days, including midnight, to catch premature next-day dedupe.
    now = datetime(2026, 10, 6, 20, 30)
    end = datetime(2026, 10, 8, 20, 30)
    while now < end:
        enqueue_free_growth(session, settings, now)
        now += timedelta(minutes=1)
    shared = session.query(Job).filter_by(job_type="free_config.shared_publish").all()
    assert len(shared) == 20
    for day in ("2026-10-07", "2026-10-08"):
        daily = [job for job in shared if job.payload["day"] == day]
        assert len(daily) == 10
        assert {job.payload["slot"] for job in daily} == set(range(1, 11))
        assert {job.idempotency_key for job in daily} == {
            f"shared_publish:{day}:{slot}" for slot in range(1, 11)
        }
    assert all(job.max_attempts == 3 for job in shared)
    draws = session.query(Job).filter_by(job_type="free_config.lottery_draw").all()
    assert {job.payload["day"] for job in draws} == {"2026-10-07", "2026-10-08"}


def test_late_utc_day_ticks_skip_missed_slot_after_tehran_midnight(session, settings, enabled_growth):
    for hour in range(24):
        enqueue_free_growth(session, settings, datetime(2026, 10, 7, hour, 35))
        enqueue_free_growth(session, settings, datetime(2026, 10, 7, hour, 36))
    shared = session.query(Job).filter_by(job_type="free_config.shared_publish").all()
    # The first tick in UTC hour 20 is already next-day 00:05 Tehran.
    # Slot 9 was missed; it must not be replayed or consume tomorrow's slot.
    assert len(shared) == 9
    assert {(job.payload["day"], job.payload["slot"]) for job in shared} == {
        ("2026-10-07", 1),
        ("2026-10-07", 2),
        ("2026-10-07", 3),
        ("2026-10-07", 4),
        ("2026-10-07", 5),
        ("2026-10-07", 6),
        ("2026-10-07", 7),
        ("2026-10-07", 8),
        ("2026-10-08", 10),
    }
    assert all(j.max_attempts == 3 for j in shared)
    draws = session.query(Job).filter_by(job_type="free_config.lottery_draw").all()
    assert len(draws) == 1
    assert draws[0].payload["day"] == "2026-10-07"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 7, 20, 29, 59), [("2026-10-07", 9)]),
        (datetime(2026, 10, 7, 20, 30), []),
        (datetime(2026, 10, 7, 20, 59, 59), []),
        (datetime(2026, 10, 7, 22, 0), [("2026-10-08", 10)]),
        (datetime(2026, 10, 8, 0, 0), []),
        (datetime(2026, 10, 8, 20, 0), [("2026-10-08", 9)]),
    ],
)
def test_shared_slot_stays_with_its_scheduled_tehran_day(session, settings, enabled_growth, now, expected):
    enqueue_free_growth(session, settings, now)
    shared = session.query(Job).filter_by(job_type="free_config.shared_publish").all()
    assert [(job.payload["day"], job.payload["slot"]) for job in shared] == expected


def test_midnight_tick_does_not_suppress_next_days_2330_slot(session, settings, enabled_growth):
    enqueue_free_growth(session, settings, datetime(2026, 10, 7, 20, 29, 59))
    enqueue_free_growth(session, settings, datetime(2026, 10, 7, 20, 30))
    shared = session.query(Job).filter_by(job_type="free_config.shared_publish").all()
    assert [(job.payload["day"], job.payload["slot"]) for job in shared] == [("2026-10-07", 9)]

    enqueue_free_growth(session, settings, datetime(2026, 10, 8, 20, 0))
    enqueue_free_growth(session, settings, datetime(2026, 10, 8, 20, 1))
    shared = session.query(Job).filter_by(job_type="free_config.shared_publish").all()
    assert {(job.payload["day"], job.payload["slot"]) for job in shared} == {
        ("2026-10-07", 9),
        ("2026-10-08", 9),
    }


def test_lottery_enqueues_at_2300_tehran_once(session, settings, enabled_growth):
    enqueue_free_growth(session, settings, datetime(2026, 10, 7, 19, 29, 59))
    assert session.query(Job).filter_by(job_type="free_config.lottery_draw").count() == 0
    enqueue_free_growth(session, settings, datetime(2026, 10, 7, 19, 30))
    enqueue_free_growth(session, settings, datetime(2026, 10, 7, 19, 59))
    draws = session.query(Job).filter_by(job_type="free_config.lottery_draw").all()
    assert len(draws) == 1
    assert draws[0].payload == {"day": "2026-10-07", "campaign_code": "pv_daily_lottery"}


def test_disabled_schedule_has_no_growth_effect_jobs(session, settings):
    enqueue_free_growth(session, settings, datetime(2026, 10, 7, 4, 0))
    assert session.query(Job).count() == 0
