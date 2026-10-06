from datetime import datetime

from pv_growth.database.models import AppConfig, Job
from pv_growth.jobs.scheduler import enqueue_free_growth


def test_shared_schedule_has_ten_slots_and_no_catchup(session, settings):
    session.add(
        AppConfig(key="pv_free_growth_policy", value={"public_shared_enabled": True, "lottery_enabled": True})
    )
    session.commit()
    for hour in range(24):
        enqueue_free_growth(session, settings, datetime(2026, 10, 7, hour, 35))
        enqueue_free_growth(session, settings, datetime(2026, 10, 7, hour, 36))
    shared = session.query(Job).filter_by(job_type="free_config.shared_publish").all()
    assert len(shared) == 10
    assert {j.payload["slot"] for j in shared} == set(range(1, 11))
    assert all(j.max_attempts == 3 for j in shared)
    draws = session.query(Job).filter_by(job_type="free_config.lottery_draw").all()
    assert len(draws) == 1
    assert draws[0].payload["day"] == "2026-10-07"


def test_disabled_schedule_has_no_growth_effect_jobs(session, settings):
    enqueue_free_growth(session, settings, datetime(2026, 10, 7, 4, 0))
    assert session.query(Job).count() == 0
