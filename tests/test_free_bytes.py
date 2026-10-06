import inspect

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.provisioning.xui import ProvisioningError, XUIProvisioningAdapter, client_email_for
from tests.test_provisioning import FakeXUI, make_adapter


def supports_bytes():
    assert "traffic_bytes" in inspect.signature(XUIProvisioningAdapter.create_temp_service).parameters, (
        "sub-GiB exact quota missing"
    )


def test_fifty_mebibytes_never_truncates_to_unlimited(settings):
    supports_bytes()
    panel = FakeXUI()
    adapter = make_adapter(panel, settings)
    result = adapter.create_temp_service(
        location="auto",
        traffic_gb=0,
        traffic_bytes=50 * 1024**2,
        validity_hours=24,
        protocol="vless",
        idempotency_key="exact",
    )
    assert panel.panel_client(result["service_ref"])["totalGB"] == 50 * 1024**2
    assert result["traffic_bytes"] == 50 * 1024**2


@pytest.mark.parametrize("quota", [0, -1, True, 1.5])
def test_invalid_exact_quota_refused_before_panel_write(settings, quota):
    supports_bytes()
    panel = FakeXUI()
    with pytest.raises(ValidationError):
        make_adapter(panel, settings).create_temp_service(
            location="auto",
            traffic_gb=0,
            traffic_bytes=quota,
            validity_hours=24,
            protocol="vless",
            idempotency_key="invalid",
        )
    assert panel.created_calls == 0


def test_replay_cannot_accept_a_different_existing_quota(settings):
    supports_bytes()
    panel = FakeXUI()
    adapter = make_adapter(panel, settings)
    adapter.create_temp_service(
        location="auto", traffic_gb=1, validity_hours=24, protocol="vless", idempotency_key="replay"
    )
    with pytest.raises(ProvisioningError):
        adapter.create_temp_service(
            location="auto",
            traffic_gb=0,
            traffic_bytes=50 * 1024**2,
            validity_hours=24,
            protocol="vless",
            idempotency_key="replay",
        )
    assert panel.created_calls == 1


def test_volume_exhaustion_is_detected_before_time_expiry(settings):
    panel = FakeXUI()
    adapter = make_adapter(panel, settings)
    adapter.create_temp_service(
        location="auto", traffic_gb=1, validity_hours=24, protocol="vless", idempotency_key="used"
    )
    panel.clients[client_email_for("used")]["down"] = 1024**3
    state = adapter.service_state(client_email_for("used"))
    assert state.get("quota_exhausted") is True
    assert state.get("traffic_used_bytes") == 1024**3
