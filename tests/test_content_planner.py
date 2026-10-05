"""Autonomous social planner acceptance tests."""

from __future__ import annotations

import uuid
from datetime import datetime

from pv_growth.database.models import AppConfig, Campaign, ContentItem, Job

NOW = datetime(2026, 10, 5, 8, 0, 0)


def _campaign(session, **updates):
    session.query(AppConfig).filter_by(key="content_planner").delete()
    values = {
        "code": f"social_{uuid.uuid4().hex[:8]}",
        "name": "Social offer",
        "kind": "purchase",
        "status": "active",
        "config": {"price": 43_721, "traffic_gb": 17, "locations": ["A", "B"]},
    }
    values.update(updates)
    row = Campaign(**values)
    session.add(row)
    session.flush()
    return row


def test_empty_campaign_data_never_fabricates_offer(session, settings):
    from pv_growth.content.planner import plan_cycle

    campaign = _campaign(session, config={})

    planned = plan_cycle(session, settings, now=NOW)

    assert [item for item in planned if item.campaign_code == campaign.code] == []


def test_planner_copies_campaign_facts_and_adds_attributable_cta(session, settings):
    from pv_growth.content.planner import plan_cycle

    campaign = _campaign(session)

    planned = [item for item in plan_cycle(session, settings, now=NOW)
               if item.campaign_code == campaign.code]

    assert len(planned) == 3
    assert {item.format for item in planned} == {"reel", "post", "story"}
    assert all(item.status == "scheduled" for item in planned)
    for item in planned:
        assert item.facts == {"price": 43_721, "traffic_gb": 17,
                              "locations": ["A", "B"]}
        assert f"https://t.me/pvnetwork_bot?start=social_{campaign.code}" in item.body
        assert "43721" not in item.body  # value stays data, body uses validated placeholders


def test_planner_uses_changed_campaign_data_without_code_change(session, settings):
    from pv_growth.content.planner import plan_cycle

    campaign = _campaign(
        session,
        config={"price": 91_337, "traffic_gb": 23, "locations": ["IR", "DE", "TR"]},
    )

    planned = [item for item in plan_cycle(session, settings, now=NOW)
               if item.campaign_code == campaign.code]

    assert planned
    assert all(item.facts["price"] == 91_337 for item in planned)
    assert all(item.facts["locations"] == ["IR", "DE", "TR"] for item in planned)


def test_planner_day_slot_dedupe_makes_repeat_cycle_idempotent(session, settings):
    from pv_growth.content.planner import plan_cycle

    campaign = _campaign(session)
    first = plan_cycle(session, settings, now=NOW)
    second = plan_cycle(session, settings, now=NOW)
    rows = session.query(ContentItem).filter_by(campaign_code=campaign.code).all()

    assert len([i for i in first if i.campaign_code == campaign.code]) == 3
    assert [i for i in second if i.campaign_code == campaign.code] == []
    assert len(rows) == 3
    assert len({row.dedupe_key for row in rows}) == 3


def test_planner_configuration_controls_hooks_without_changing_facts(session, settings):
    from pv_growth.content.planner import plan_cycle

    campaign = _campaign(session)
    session.merge(AppConfig(key="content_planner", value={
        "hooks": ["HOOK FROM DATA"],
        "slots_utc": [10],
        "formats": ["post"],
    }))
    session.flush()

    planned = [item for item in plan_cycle(session, settings, now=NOW)
               if item.campaign_code == campaign.code]

    assert len(planned) == 1
    assert planned[0].title == "HOOK FROM DATA"
    assert planned[0].facts["price"] == 43_721


def test_trend_provider_failure_falls_back_without_changing_commercial_facts(session, settings):
    from pv_growth.content.planner import plan_cycle

    class _BrokenTrendProvider:
        def suggestions(self, session, *, limit=5):
            raise RuntimeError("provider unavailable")

    campaign = _campaign(session)
    planned = [item for item in plan_cycle(
        session, settings, now=NOW, trend_provider=_BrokenTrendProvider()
    ) if item.campaign_code == campaign.code]

    assert len(planned) == 3
    assert all(item.facts["price"] == 43_721 for item in planned)
    assert all(item.creative["trend_source"] == "evergreen" for item in planned)


def test_plan_job_registered_and_scheduler_enqueue_deduplicated(settings, session, monkeypatch):
    from pv_growth.jobs import runner
    from pv_growth.jobs.scheduler import Scheduler

    runner.register_builtin_handlers()
    assert "content.plan" in runner._HANDLERS

    before = {row.id for row in session.query(Job).all()}
    monkeypatch.setattr("pv_growth.jobs.scheduler.time.time", lambda: 1_000_000)
    scheduler = Scheduler(settings)
    scheduler._enqueue_periodic()
    scheduler._enqueue_periodic()

    rows = session.query(Job).filter_by(job_type="content.plan").all()
    assert len(rows) == 1
    assert rows[0].idempotency_key.startswith("content_plan:")
    for row in session.query(Job).all():
        if row.id not in before:
            session.delete(row)
    session.flush()
