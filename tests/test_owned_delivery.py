import importlib
import importlib.util

import httpx
import pytest

from pv_growth.config_quality.probe import ProbeResult
from pv_growth.core.errors import ValidationError


def module():
    assert importlib.util.find_spec("pv_growth.free_config.owned_delivery"), (
        "owned subscription verification missing"
    )
    return importlib.import_module("pv_growth.free_config.owned_delivery")


def test_subscription_can_only_use_authorized_origin(settings):
    p = module()
    s = settings.model_copy(update={"provisioning_sublink": "https://sub.example/link"})
    for url in ("https://evil.example/link/id", "http://sub.example/link/id", "https://sub.example/other/id"):
        with pytest.raises(ValidationError):
            p.verified_uri({"config_uri": url}, s)


def test_subscription_must_contain_a_working_protocol_not_a_label(settings, monkeypatch):
    p = module()
    s = settings.model_copy(update={"provisioning_sublink": "https://sub.example/link"})
    monkeypatch.setattr(p, "probe_uri", lambda uri, s: ProbeResult(False, "https_probe_failed"))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="vless://id@8.8.8.8:443"))
    with pytest.raises(ValidationError):
        p.verified_uri(
            {"config_uri": "https://sub.example/link/id", "client_id": "id", "sub_id": "id"},
            s,
            transport=transport,
        )


def test_a_verified_uri_is_delivered_only_after_actual_probe(settings, monkeypatch):
    p = module()
    s = settings.model_copy(update={"provisioning_sublink": "https://sub.example/link"})
    uri = "vless://id@8.8.8.8:443"
    monkeypatch.setattr(p, "probe_uri", lambda uri, s: ProbeResult(True, "https_via_proxy", 10))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=uri))
    assert (
        p.verified_uri(
            {"config_uri": "https://sub.example/link/id", "client_id": "id", "sub_id": "id"},
            s,
            transport=transport,
        )[0]
        == uri
    )


@pytest.mark.parametrize("client_id,sub_id", [(None, "id"), ("other", "id"), ("id", "other")])
def test_subscription_identity_must_match_proven_client(settings, monkeypatch, client_id, sub_id):
    p = module()
    s = settings.model_copy(update={"provisioning_sublink": "https://sub.example/link"})
    calls = []
    monkeypatch.setattr(
        p, "probe_uri", lambda uri, s: calls.append(uri) or ProbeResult(True, "https_via_proxy", 10)
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="vless://id@8.8.8.8:443"))
    with pytest.raises(ValidationError):
        p.verified_uri(
            {"config_uri": "https://sub.example/link/id", "client_id": client_id, "sub_id": sub_id},
            s,
            transport=transport,
        )
    assert not calls


@pytest.mark.parametrize(
    "subscription_id", ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
)
def test_authoritative_panel_get_binds_subscription_to_quota_client(settings, monkeypatch, subscription_id):
    from pv_growth.provisioning.xui import XUIProvisioningAdapter, client_email_for

    p = module()
    expected = "11111111-1111-4111-8111-111111111111"
    email = client_email_for("integration-test")
    s = settings.model_copy(
        update={
            "provisioning_base_url": "https://panel.example",
            "provisioning_token": "test",
            "provisioning_sublink": "https://sub.example/link",
        }
    )
    client = {
        "email": email,
        "id": expected,
        "subId": "owned-sub",
        "totalGB": 100 * 1024**2,
        "expiryTime": 123,
        "enable": True,
    }
    panel = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"success": True, "obj": {"client": client, "usedTraffic": 0}}
        )
    )
    adapter = XUIProvisioningAdapter(s, transport=panel)
    service = adapter._service_payload(email, adapter._get_or_none(email))
    assert service["client_id"] == expected and service["sub_id"] == "owned-sub"
    p.require_client_identity(service, adapter.service_state(email))
    monkeypatch.setattr(p, "probe_uri", lambda *args: ProbeResult(True, "https_via_proxy", 10))
    subscription = httpx.MockTransport(
        lambda request: httpx.Response(200, text=f"vless://{subscription_id}@8.8.8.8:443")
    )
    if subscription_id == expected:
        assert p.verified_uri(service, s, transport=subscription)[0].startswith("vless://" + expected)
    else:
        with pytest.raises(ValidationError):
            p.verified_uri(service, s, transport=subscription)


@pytest.mark.parametrize("used", [0, 100 * 1024**2])
def test_production_panel_numeric_row_id_uuid_and_aggregate_usage(settings, used):
    from pv_growth.provisioning.xui import XUIProvisioningAdapter, client_email_for

    email = client_email_for("production-shape")
    expected = "11111111-1111-4111-8111-111111111111"
    client = {
        "email": email,
        "id": 1234,
        "uuid": expected,
        "subId": "owned-sub",
        "totalGB": 100 * 1024**2,
        "expiryTime": 123,
        "enable": True,
    }
    panel = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"success": True, "obj": {"client": client, "usedTraffic": used}}
        )
    )
    adapter = XUIProvisioningAdapter(
        settings.model_copy(
            update={
                "provisioning_base_url": "https://panel.example",
                "provisioning_token": "test",
                "provisioning_sublink": "https://sub.example/link",
            }
        ),
        transport=panel,
    )
    service = adapter._service_payload(email, adapter._get_or_none(email))
    state = adapter.service_state(email)
    assert service["client_id"] == state["client_id"] == expected
    module().require_client_identity(service, state)
    assert state["traffic_used_bytes"] == used
    assert state["quota_exhausted"] is (used >= 100 * 1024**2)


@pytest.mark.parametrize("used", [None, -1, "0", True])
def test_production_panel_invalid_usage_fails_closed(settings, used):
    from pv_growth.provisioning.xui import ProvisioningError, XUIProvisioningAdapter, client_email_for

    email = client_email_for("unproven-usage")
    client = {
        "email": email,
        "id": 1234,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "subId": "owned-sub",
        "totalGB": 100 * 1024**2,
        "enable": True,
    }
    panel = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"success": True, "obj": {"client": client, "usedTraffic": used}}
        )
    )
    adapter = XUIProvisioningAdapter(
        settings.model_copy(
            update={"provisioning_base_url": "https://panel.example", "provisioning_token": "test"}
        ),
        transport=panel,
    )
    with pytest.raises(ProvisioningError):
        adapter.service_state(email)


@pytest.mark.parametrize("value", [None, "", 1234, "not-a-uuid", True])
def test_explicit_invalid_panel_uuid_cannot_fall_back_to_legacy_id(value):
    from pv_growth.provisioning.xui import _client_uuid

    assert _client_uuid({"uuid": value, "id": "11111111-1111-4111-8111-111111111111"}) is None
    assert _client_uuid({"id": 1234}) is None


def test_legacy_panel_byte_usage_remains_supported_without_aggregate():
    from pv_growth.provisioning.xui import _used_bytes

    assert _used_bytes({"up": 12, "down": 13}, {}) == 25
    assert _used_bytes({"client": {"up": 12, "down": 13}}, {"up": 12, "down": 13}) == 25
    # Explicit unproven aggregate cannot be replaced by a convenient legacy 0.
    from pv_growth.provisioning.xui import ProvisioningError

    with pytest.raises(ProvisioningError):
        _used_bytes({"usedTraffic": None, "up": 0, "down": 0}, {})
