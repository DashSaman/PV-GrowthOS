import httpx

from pv_growth.competitors.watcher import check_source, run_watch
from pv_growth.core.flags import FlagService
from pv_growth.database.models import CompetitorSource, Job
from pv_growth.jobs.scheduler import Scheduler


def test_competitor_watch_is_registered_and_scheduled_once_per_six_hours(session, settings, monkeypatch):
    from pv_growth.jobs import runner

    monkeypatch.setattr("pv_growth.jobs.scheduler.time.time", lambda: 1791761400)
    configured = settings.model_copy(update={"flag_competitor_watch_enabled": True})
    runner.register_builtin_handlers()
    assert "competitors.watch" in runner._HANDLERS
    Scheduler(configured)._enqueue_periodic()
    Scheduler(configured)._enqueue_periodic()
    assert session.query(Job).filter_by(job_type="competitors.watch").count() == 1


def test_disabled_competitor_watch_is_not_scheduled(session, settings):
    Scheduler(settings)._enqueue_periodic()
    assert session.query(Job).filter_by(job_type="competitors.watch").count() == 0


def test_competitor_watch_limits_sources_and_response_bytes(session, settings):
    for i in range(12):
        session.add(CompetitorSource(name=f"rival{i}", url="https://t.me/s/example", fields={}))
    session.flush()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x" * 1_048_577)))
    flags = FlagService(settings.model_copy(update={"flag_competitor_watch_enabled": True}))
    result = run_watch(session, settings, flags, client)
    assert result == {"sources": 10, "changes": 0, "failures": 10}
    assert not client.is_closed
    client.close()


def test_invalid_source_pattern_is_isolated(session):
    source = CompetitorSource(name="bad", url="https://t.me/s/example", fields={"broken": {"regex": "["}})
    session.add(source)
    session.flush()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="hi"))) as client:
        assert check_source(session, source, client) == -1
