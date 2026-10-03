"""Config URI parsers: vmess / vless / trojan / ss (SIP002).

Produce a normalized record (protocol, host, port, credential fingerprint,
remark, raw uri). The credential fingerprint hashes the secret so dedupe
works without comparing secrets directly. No network I/O here.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

SUPPORTED_PROTOCOLS = ("vmess", "vless", "trojan", "ss")


@dataclass(frozen=True)
class ParsedConfig:
    protocol: str
    host: str
    port: int
    credential: str          # uuid / password / method:password
    credential_fp: str       # sha256(credential)[:16]
    remark: str
    raw_uri: str
    tls: bool = False
    extra: dict | None = None

    @property
    def uri_hash(self) -> str:
        """Identity for dedup: same protocol+host+port+credential == same config,
        regardless of remark cosmetics or source."""
        basis = f"{self.protocol}|{self.host.lower()}|{self.port}|{self.credential_fp}"
        return hashlib.sha256(basis.encode()).hexdigest()


def _b64decode(data: str) -> bytes:
    data = data.strip().replace("-", "+").replace("_", "/")
    data += "=" * (-len(data) % 4)
    return base64.b64decode(data, validate=False)


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def parse_vmess(uri: str) -> ParsedConfig | None:
    try:
        payload = json.loads(_b64decode(uri[len("vmess://"):]).decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError):
        return None
    host = str(payload.get("add", "")).strip()
    try:
        port = int(payload.get("port", 0))
    except (TypeError, ValueError):
        return None
    cred = str(payload.get("id", ""))
    if not host or not cred or not port:
        return None
    return ParsedConfig(
        protocol="vmess", host=host, port=port, credential=cred,
        credential_fp=_fingerprint(cred), remark=str(payload.get("ps", "")),
        raw_uri=uri, tls=str(payload.get("tls", "")).lower() in {"tls", "reality"},
        extra={"net": payload.get("net"), "path": payload.get("path")},
    )


def parse_userinfo_uri(uri: str, protocol: str) -> ParsedConfig | None:
    """vless://uuid@host:port?params#remark and trojan://pass@host:port#remark"""
    try:
        parts = urlparse(uri)
        port = parts.port  # raises ValueError when out of range
    except ValueError:
        return None
    if not parts.hostname or not parts.username or not port:
        return None
    return ParsedConfig(
        protocol=protocol, host=parts.hostname, port=port,
        credential=unquote(parts.username), credential_fp=_fingerprint(unquote(parts.username)),
        remark=unquote(parts.fragment or ""), raw_uri=uri,
        tls=protocol in {"vless", "trojan"} or parts.scheme.endswith("s"),
        extra={k: v[0] for k, v in parse_qs(parts.query).items()} or None,
    )


def parse_ss(uri: str) -> ParsedConfig | None:
    """ss:// SIP002: base64(method:pass)@host:port#remark  (or legacy full-b64)."""
    body = uri[len("ss://"):]
    remark = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        remark = unquote(frag)
    try:
        if "@" not in body:  # legacy: whole thing is base64
            decoded = _b64decode(body).decode("utf-8", "replace")
            userinfo, hostport = decoded.rsplit("@", 1)
        else:
            userinfo_b64, hostport = body.rsplit("@", 1)
            userinfo = _b64decode(userinfo_b64).decode("utf-8", "replace")
        if ":" not in hostport:
            return None
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
        method, _, password = userinfo.partition(":")
        if not method or not password or not host:
            return None
    except (ValueError, IndexError):
        return None
    return ParsedConfig(
        protocol="ss", host=host, port=port, credential=f"{method}:{password}",
        credential_fp=_fingerprint(f"{method}:{password}"), remark=remark,
        raw_uri=uri, tls=False,
    )


def parse_any(uri: str) -> ParsedConfig | None:
    uri = uri.strip()
    if uri.startswith("vmess://"):
        return parse_vmess(uri)
    if uri.startswith("vless://"):
        return parse_userinfo_uri(uri, "vless")
    if uri.startswith("trojan://"):
        return parse_userinfo_uri(uri, "trojan")
    if uri.startswith("ss://"):
        return parse_ss(uri)
    return None


PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10",
))


def is_private_host(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False  # a domain name; DNS-resolved filtering happens in health check
    return any(addr in net for net in PRIVATE_NETS)


def validate(parsed: ParsedConfig, *, allow_private: bool = False) -> str | None:
    """Return None when valid, else a human-readable invalid reason."""
    if parsed.protocol not in SUPPORTED_PROTOCOLS:
        return f"unsupported protocol {parsed.protocol}"
    if not (1 <= parsed.port <= 65535):
        return f"bad port {parsed.port}"
    if not parsed.host or len(parsed.host) > 253:
        return "bad host"
    if not parsed.credential_fp:
        return "missing credential"
    if not allow_private and is_private_host(parsed.host):
        return "private/loopback host"
    if parsed.port in {22, 80, 443} and parsed.protocol == "ss":
        return None  # allow common ports for ss
    return None


def score(parsed: ParsedConfig, source_reliability: float = 0.5) -> float:
    """Quality score in [0,1]: reliability + completeness + tls bonus."""
    base = 0.5 * max(0.0, min(1.0, source_reliability))
    completeness = 0.3 * (
        0.4 * bool(parsed.remark) + 0.3 * bool(parsed.tls) + 0.3 * bool(parsed.extra)
    )
    rarity = 0.2 * (0.5 if parsed.protocol in {"vmess", "ss"} else 1.0)
    return round(min(1.0, base + completeness + rarity), 4)
