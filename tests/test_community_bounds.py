import httpx
import pytest

from pv_growth.config_sources.fetchers import fetch_github_file


def test_out_of_range_community_id_is_unavailable(session, settings):
    from pv_growth.free_config.community import current_config

    assert current_config(session, settings, int("9" * 40)) is None


def test_public_source_rejects_body_larger_than_one_mib():
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (1048576 + 1)))
    ) as client:
        with pytest.raises(ValueError, match="source too large"):
            fetch_github_file("https://raw.githubusercontent.com/example/list.txt", client)


def test_production_probes_fourth_candidate_when_first_three_fail(session, settings, monkeypatch):
    from pv_growth.config_quality.probe import ProbeResult
    from pv_growth.database.models import ConfigSource, RawConfig
    from pv_growth.free_config import pipeline

    source = ConfigSource(name="fresh", kind="github_file", url="https://example.test/list")
    session.add(source)
    session.flush()
    rows = [
        RawConfig(
            uri_hash=str(i),
            source_id=source.id,
            protocol="vless",
            host="8.8.8.8",
            port=443,
            raw_uri=f"candidate-{i}",
            valid=True,
            status="validated",
            quality_score=100 - i,
        )
        for i in range(4)
    ]
    session.add_all(rows)
    session.commit()
    monkeypatch.setattr(pipeline, "check_top_candidates", lambda rs, s: {r[0]: True for r in rs})
    monkeypatch.setattr(
        "pv_growth.config_quality.probe.probe_uri",
        lambda uri, s: ProbeResult(uri == "candidate-3", "https_via_proxy"),
    )
    ranked = pipeline.rank_candidates(
        session, settings.model_copy(update={"env": "production", "health_check_max": 6})
    )
    assert [r.raw_uri for r in ranked] == ["candidate-3"]
