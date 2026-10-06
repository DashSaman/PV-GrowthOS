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
        lambda request: httpx.Response(200, json={"success": True, "obj": {"client": client}})
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
