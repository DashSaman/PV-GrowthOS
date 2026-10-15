"""Production candidate starvation must not spend the whole budget on one source."""

from pv_growth.database.models import ConfigSource, RawConfig
from pv_growth.database.types import utcnow
from pv_growth.free_config.pipeline import rank_candidates


def source(session, name, active=True):
    row = ConfigSource(kind="github_file", name=name, url=f"https://example.test/{name}", is_active=active)
    session.add(row)
    session.flush()
    return row


def candidate(session, src, index, *, quality=1.0, checked=False, healthy=None, valid=True):
    row = RawConfig(
        source_id=src.id,
        uri_hash=f"source-{src.id}-config-{index}",
        protocol="vless",
        host="example.test",
        port=443,
        raw_uri=f"vless://fixture-{index}@example.test:443",
        valid=valid,
        fetched_at=utcnow(),
        status="validated",
        quality_score=quality,
        health_checked=checked,
        health_ok=healthy,
    )
    session.add(row)
    session.flush()
    return row


def test_fresh_lower_score_source_can_satisfy_slot_despite_large_dead_source(session, settings, monkeypatch):
    dominant = source(session, "dominant")
    fallback = source(session, "fresh-fallback")
    for i in range(30):
        candidate(session, dominant, i)
    good = candidate(session, fallback, 100, quality=0.7)
    seen = []

    def check(rows, _settings):
        seen.extend(cid for cid, _host, _port in rows)
        return {cid: cid == good.id for cid, _host, _port in rows}

    monkeypatch.setattr("pv_growth.free_config.pipeline.check_top_candidates", check)
    monkeypatch.setattr(
        "pv_growth.config_quality.probe.probe_uri", lambda *_: type("Result", (), {"ok": True})()
    )
    ranked = rank_candidates(
        session, settings.model_copy(update={"env": "production", "health_check_max": 6})
    )
    assert [r.id for r in ranked] == [good.id]
    assert len(seen) <= 6


def test_known_healthy_candidates_keep_priority_and_disabled_invalid_excluded(session, settings, monkeypatch):
    primary = source(session, "healthy")
    secondary = source(session, "untested")
    disabled = source(session, "disabled", active=False)
    good = [candidate(session, primary, i, quality=0.4, checked=True, healthy=True) for i in range(6)]
    candidate(session, secondary, 50, quality=1.0)
    candidate(session, disabled, 60, checked=True, healthy=True)
    candidate(session, secondary, 70, checked=True, healthy=True, valid=False)
    seen = []

    def check(rows, _settings):
        seen.extend(cid for cid, _host, _port in rows)
        return {cid: True for cid, _host, _port in rows}

    monkeypatch.setattr("pv_growth.free_config.pipeline.check_top_candidates", check)
    monkeypatch.setattr(
        "pv_growth.config_quality.probe.probe_uri", lambda *_: type("Result", (), {"ok": True})()
    )
    ranked = rank_candidates(
        session, settings.model_copy(update={"env": "production", "health_check_max": 6})
    )
    assert set(seen) == {r.id for r in good}
    assert len(ranked) == 6


def test_each_unchecked_source_is_reached_within_unchanged_probe_batch(session, settings, monkeypatch):
    sources = [source(session, f"pool-{i}") for i in range(4)]
    for i, src in enumerate(sources):
        for j in range(10):
            candidate(session, src, j, quality=1.0 - i / 10)
    selected = []

    def check(rows, _settings):
        selected.extend(cid for cid, _host, _port in rows)
        return {cid: False for cid, _host, _port in rows}

    monkeypatch.setattr("pv_growth.free_config.pipeline.check_top_candidates", check)
    excluded = [r.id for r in session.query(RawConfig).filter_by(source_id=sources[0].id).limit(2)]
    rank_candidates(
        session,
        settings.model_copy(update={"env": "production", "health_check_max": 6}),
        exclude_ids=excluded,
    )
    rows = session.query(RawConfig).filter(RawConfig.id.in_(selected)).all()
    assert {r.source_id for r in rows} == {s.id for s in sources}
    assert len(selected) == 6
    assert not set(selected) & set(excluded)
