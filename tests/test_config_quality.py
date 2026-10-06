"""Phase 2: parser correctness, validation, dedup hashing, scoring."""

import base64
import json
import socket

from pv_growth.config_quality.health import tcp_check
from pv_growth.config_quality.parsers import is_private_host, parse_any, score, validate


def _vmess_uri(host="1.2.3.4", port=443, uid="aaaa-bbbb-cccc", remark="Test", tls="tls"):
    payload = {
        "v": "2",
        "ps": remark,
        "add": host,
        "port": str(port),
        "id": uid,
        "aid": "0",
        "net": "ws",
        "path": "/ws",
        "tls": tls,
    }
    return "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()


def test_parse_vmess():
    parsed = parse_any(_vmess_uri())
    assert parsed and parsed.protocol == "vmess"
    assert parsed.host == "1.2.3.4" and parsed.port == 443
    assert parsed.remark == "Test" and parsed.tls is True


def test_parse_vless_trojan_ss():
    vless = parse_any("vless://uuid-123@de.example.com:443?security=tls#DE%20Node")
    assert vless and vless.protocol == "vless" and vless.port == 443
    assert vless.remark == "DE Node"

    trojan = parse_any("trojan://secretpw@1.2.3.5:8443#Trojan")
    assert trojan and trojan.protocol == "trojan" and trojan.credential == "secretpw"

    userinfo = base64.b64encode(b"aes-256-gcm:pass123").decode()
    ss = parse_any(f"ss://{userinfo}@5.6.7.8:8388#SS")
    assert ss and ss.protocol == "ss" and ss.port == 8388


def test_invalid_configs_rejected():
    # corrupt base64
    assert parse_any("vmess://!!!not-base64!!!") is None
    # private host
    parsed = parse_any(_vmess_uri(host="192.168.1.1"))
    assert validate(parsed) == "private/loopback host"
    # loopback
    parsed = parse_any(_vmess_uri(host="127.0.0.1"))
    assert validate(parsed) == "private/loopback host"
    # bad port
    parsed = parse_any("vless://u@h.test:99999#x")
    assert parsed is None or validate(parsed) is not None
    # missing credential
    parsed = parse_any("trojan://@1.2.3.4:443#x")
    assert parsed is None


def test_dedup_hash_ignores_remark_not_credentials():
    a = parse_any(_vmess_uri(remark="Server A"))
    b = parse_any(_vmess_uri(remark="Server B"))  # same endpoint, other remark
    c = parse_any(_vmess_uri(uid="different-uid"))  # same endpoint, other credential
    assert a.uri_hash == b.uri_hash  # cosmetic differences deduped
    assert a.uri_hash != c.uri_hash  # different credential = different config


def test_scoring_bounds_and_reliability():
    parsed = parse_any(_vmess_uri())
    low = score(parsed, source_reliability=0.1)
    high = score(parsed, source_reliability=0.9)
    assert 0.0 <= low < high <= 1.0


def test_is_private_host_domains_are_not_flagged():
    assert is_private_host("10.0.0.5") is True
    assert is_private_host("example.com") is False  # DNS filtering is health-check's job


def test_tcp_health_rejects_domain_resolving_private(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.23.77.1", 443))],
    )
    attempted = []
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: attempted.append(args))

    assert tcp_check("attacker.example", 443, 0.1) is False
    assert attempted == []


def test_tcp_health_connects_only_to_resolved_public_ip(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))],
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    attempted = []

    def _connect(address, timeout):
        attempted.append((address, timeout))
        return _Connection()

    monkeypatch.setattr(socket, "create_connection", _connect)

    assert tcp_check("public.example", 443, 0.1) is True
    assert attempted == [(("1.1.1.1", 443), 0.1)]
