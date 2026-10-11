from datetime import timedelta

import httpx

from pv_growth.config_quality.probe import ProbeResult
from pv_growth.database.models import ConfigSource, RawConfig
from pv_growth.database.types import utcnow
from pv_growth.free_config import pipeline

URI = "vless://00000000-0000-4000-8000-000000000001@8.8.8.8:443?security=tls&sni=example.com"


def test_failed_candidate_does_not_starve_unchecked_candidates(session, settings, monkeypatch):
    source = ConfigSource(name="public", kind="github_file", url="https://example.test/list")
    session.add(source)
    session.flush()
    failed = RawConfig(
        source_id=source.id,
        uri_hash="failed",
        raw_uri="failed",
        protocol="vless",
        host="8.8.8.8",
        port=443,
        valid=True,
        status="validated",
        quality_score=100,
        health_checked=True,
        health_ok=False,
    )
    unchecked = RawConfig(
        source_id=source.id,
        uri_hash="unchecked",
        raw_uri="unchecked",
        protocol="vless",
        host="8.8.8.8",
        port=443,
        valid=True,
        status="validated",
        quality_score=10,
        health_checked=False,
    )
    session.add_all([failed, unchecked])
    session.commit()
    monkeypatch.setattr(pipeline, "check_top_candidates", lambda rows, s: {r[0]: True for r in rows})
    monkeypatch.setattr(
        "pv_growth.config_quality.probe.probe_uri",
        lambda uri, s: ProbeResult(uri == "unchecked", "https_via_proxy"),
    )
    ranked = pipeline.rank_candidates(
        session, settings.model_copy(update={"env": "production", "health_check_max": 1})
    )
    assert [r.uri_hash for r in ranked] == ["unchecked"]


def test_published_candidate_is_freshly_retested_for_later_slot(session, settings, monkeypatch):
    from pv_growth.config_quality.parsers import parse_any

    source = ConfigSource(name="public", kind="github_file", url="https://example.test/list")
    session.add(source)
    session.flush()
    row = RawConfig(
        source_id=source.id,
        uri_hash=parse_any(URI).uri_hash,
        raw_uri=URI,
        protocol="vless",
        host="8.8.8.8",
        port=443,
        valid=True,
        status="published",
        quality_score=100,
        health_checked=True,
        health_ok=True,
        published_at=utcnow() - timedelta(hours=18),
        fetched_at=utcnow() - timedelta(hours=25),
    )
    session.add(row)
    session.commit()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=URI))) as http:
        pipeline.collect_and_stage(session, http, settings)
    session.commit()
    assert row.fetched_at > utcnow() - timedelta(minutes=1)
    assert row.status == "published", "refresh must not invalidate the existing public receipt"
    checked = []
    monkeypatch.setattr(pipeline, "check_top_candidates", lambda rows, s: {r[0]: True for r in rows})

    def probe(uri, s):
        checked.append(uri)
        return ProbeResult(True, "https_via_proxy")

    monkeypatch.setattr("pv_growth.config_quality.probe.probe_uri", probe)
    ranked = pipeline.rank_candidates(
        session, settings.model_copy(update={"env": "production", "health_check_max": 1})
    )
    assert [r.id for r in ranked] == [row.id]
    assert checked == [URI], "reuse requires a new actual HTTPS probe"
