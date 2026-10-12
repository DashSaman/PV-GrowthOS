from datetime import datetime

import httpx
import pytest

from pv_growth.config_quality.parsers import parse_any
from pv_growth.database.models import ConfigSource, RawConfig
from pv_growth.free_config.pipeline import collect_and_stage

URI = "trojan://test-password@8.8.8.8:443?security=tls&sni=example.com#old"


@pytest.mark.parametrize("status", ["validated", "published", "probe_failed"])
def test_fresh_approved_source_adopts_config_from_inactive_registry(session, settings, status):
    old = ConfigSource(
        name="retired source", kind="github_file", url="https://old.example/pool", is_active=False
    )
    fresh = ConfigSource(
        name="approved source", kind="github_file", url="https://fresh.example/pool", is_active=True
    )
    session.add_all([old, fresh])
    session.flush()
    parsed = parse_any(URI)
    row = RawConfig(
        source_id=old.id,
        uri_hash=parsed.uri_hash,
        protocol=parsed.protocol,
        host=parsed.host,
        port=parsed.port,
        raw_uri=URI,
        valid=True,
        quality_score=0.9,
        status=status,
        health_checked=True,
        health_ok=True,
        fetched_at=datetime(2026, 10, 6),
    )
    session.add(row)
    session.commit()
    original_id = row.id
    updated = URI.replace("#old", "#fresh")
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=updated))
    ) as client:
        stats = collect_and_stage(session, client, settings)
    assert row.id == original_id and session.query(RawConfig).count() == 1
    assert stats["new"] == 0 and stats["duplicates"] == 1
    assert row.source_id == fresh.id
    assert row.raw_uri == updated and row.status == "validated"
    assert row.health_checked is False and row.health_ok is None


def test_source_adoption_preserves_explicit_invalid_quarantine(session, settings):
    old = ConfigSource(name="retired", kind="github_file", url="https://old.example/pool", is_active=False)
    fresh = ConfigSource(
        name="approved", kind="github_file", url="https://fresh.example/pool", is_active=True
    )
    session.add_all([old, fresh])
    session.flush()
    p = parse_any(URI)
    row = RawConfig(
        source_id=old.id,
        uri_hash=p.uri_hash,
        protocol=p.protocol,
        host=p.host,
        port=p.port,
        raw_uri=URI,
        valid=False,
        status="validated",
    )
    session.add(row)
    session.commit()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=URI))) as client:
        collect_and_stage(session, client, settings)
    assert row.valid is False
