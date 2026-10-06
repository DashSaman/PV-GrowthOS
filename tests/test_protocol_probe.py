"""A reachable port is not evidence a VPN can carry authenticated HTTPS."""

import base64
import importlib
import importlib.util
import json

import pytest


def module():
    assert importlib.util.find_spec("pv_growth.config_quality.probe"), "actual protocol probe is missing"
    return importlib.import_module("pv_growth.config_quality.probe")


def vmess():
    obj = {
        "add": "edge.example",
        "port": "443",
        "id": "c7ef5d47-df38-4127-a246-8eab1d6135e3",
        "net": "ws",
        "tls": "tls",
        "host": "cdn.example",
        "sni": "tls.example",
        "path": "/ws",
    }
    return "vmess://" + base64.b64encode(json.dumps(obj).encode()).decode()


def test_pinned_ip_keeps_transport_and_tls_identity():
    config = module().build_probe_config(vmess(), "8.8.8.8", 23456, "probe", "secret")
    out = config["outbounds"][0]
    assert out["settings"]["vnext"][0]["address"] == "8.8.8.8"
    assert out["streamSettings"]["tlsSettings"]["serverName"] == "tls.example"
    assert out["streamSettings"]["wsSettings"]["headers"]["Host"] == "cdn.example"
    assert config["inbounds"][0]["listen"] == "127.0.0.1"
    assert config["inbounds"][0]["settings"]["accounts"] == [{"user": "probe", "pass": "secret"}]
    assert all(o["protocol"] != "freedom" for o in config["outbounds"])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.2.3.4", "169.254.169.254", "::1"])
def test_private_vetted_address_never_accepted(address):
    with pytest.raises(ValueError):
        module().build_probe_config(vmess(), address, 23456, "probe", "secret")


@pytest.mark.parametrize(
    "query",
    [
        "type=evil",
        "type=tcp&security=tls&allowInsecure=1",
        "type=ws&security=tls&host=x%0D%0AHeader:bad",
        "type=xhttp&security=reality",
    ],
)
def test_unsupported_or_unsafe_transport_is_fail_closed(query):
    uri = "vless://c7ef5d47-df38-4127-a246-8eab1d6135e3@8.8.8.8:443?" + query
    with pytest.raises(ValueError):
        module().build_probe_config(uri, "8.8.8.8", 23456, "probe", "secret")


def test_heterogeneous_dns_cannot_rebind_to_private(monkeypatch):
    p = module()
    monkeypatch.setattr(
        p.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))],
    )
    assert p.vetted_addresses("edge.example", 443) == []


def test_missing_core_never_promotes_tcp_success(settings):
    p = module()
    s = settings.model_copy(update={"proxy_probe_binary": "/missing-xray"})
    assert p.probe_uri(vmess(), s).ok is False


def test_busy_probe_cannot_spawn_more_clients(settings, monkeypatch):
    import pv_growth.config_quality.probe as p

    class Busy:
        def acquire(self, **kwargs):
            return False

    monkeypatch.setattr(p, "_PROBE_SLOTS", Busy())
    monkeypatch.setattr(
        p.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("spawned past cap"))
    )
    assert p.probe_uri("vless://id@8.8.8.8:443", settings).reason == "probe_busy"
