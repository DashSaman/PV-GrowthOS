from datetime import date

from pv_growth.config_quality.probe import ProbeResult
from pv_growth.core.flags import FlagService
from pv_growth.database.models import ConfigSource, RawConfig
from pv_growth.free_config.pipeline import publish_public_slot
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def test_production_tcp_only_candidate_cannot_be_published(session, settings, monkeypatch):
    source = ConfigSource(kind="github_file", name="source", url="https://example.com/feed")
    session.add(source)
    session.flush()
    session.add(
        RawConfig(
            source_id=source.id,
            uri_hash="test",
            protocol="vless",
            host="8.8.8.8",
            port=443,
            raw_uri="vless://id@8.8.8.8:443",
            status="validated",
        )
    )
    session.flush()
    monkeypatch.setattr(
        "pv_growth.free_config.pipeline.check_top_candidates", lambda rows, s: {r[0]: True for r in rows}
    )
    monkeypatch.setattr(
        "pv_growth.config_quality.probe.probe_uri", lambda uri, s: ProbeResult(False, "https_probe_failed")
    )
    s = settings.model_copy(
        update={
            "env": "production",
            "flag_free_config_enabled": True,
            "flag_public_config_enabled": True,
            "free_channel_id": "@free",
        }
    )
    transport = FakeTelegramTransport()
    assert (
        publish_public_slot(session, s, FlagService(s), TelegramClient(transport, s), day=date(2026, 10, 6))
        is None
    )
    assert not transport.calls
