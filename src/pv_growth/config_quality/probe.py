"""Bounded real HTTPS through a temporary proxy CLIENT; no direct fallback."""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from pv_growth.config_quality.parsers import _b64decode, parse_any, validate
from pv_growth.core.config import Settings

TARGETS = ("https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/generate_204")
_PROBE_SLOTS = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    reason: str
    latency_ms: int | None = None


def vetted_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = list(
            dict.fromkeys(i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        )
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            return []
        return addresses
    except (OSError, ValueError):
        return []


def _safe(value) -> str:
    value = str(value or "")
    if len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("unsafe transport value")
    return value


def build_probe_config(uri: str, address: str, local_port: int, username: str, password: str) -> dict:
    parsed = parse_any(uri)
    if parsed is None or validate(parsed) or not ipaddress.ip_address(address).is_global:
        raise ValueError("invalid public config")
    if parsed.protocol == "vmess":
        params = json.loads(_b64decode(uri[len("vmess://") :]))
        network = _safe(params.get("net") or "tcp")
        security = _safe(params.get("tls") or "none")
    else:
        params = {k: v[0] for k, v in parse_qs(urlparse(uri).query).items()}
        network = _safe(params.get("type") or "tcp")
        security = _safe(params.get("security") or ("tls" if parsed.protocol == "trojan" else "none"))
    if network not in {"tcp", "ws", "grpc", "httpupgrade"} or security not in {"none", "tls", "reality"}:
        raise ValueError("unsupported transport")
    if str(params.get("allowInsecure", "")).lower() in {"1", "true"} or params.get("plugin"):
        raise ValueError("unsafe or unsupported transport")
    stream: dict = {"network": network, "security": security}
    if security == "tls":
        stream["tlsSettings"] = {
            "serverName": _safe(params.get("sni") or params.get("serverName") or parsed.host),
            "allowInsecure": False,
        }
        if params.get("alpn"):
            stream["tlsSettings"]["alpn"] = _safe(params["alpn"]).split(",")
        if params.get("fp"):
            stream["tlsSettings"]["fingerprint"] = _safe(params["fp"])
    elif security == "reality":
        if not params.get("pbk") or not params.get("sni"):
            raise ValueError("incomplete reality")
        stream["realitySettings"] = {
            "serverName": _safe(params["sni"]),
            "fingerprint": _safe(params.get("fp") or "chrome"),
            "publicKey": _safe(params["pbk"]),
            "shortId": _safe(params.get("sid")),
        }
    host, path = _safe(params.get("host") or parsed.host), _safe(params.get("path") or "/")
    if network == "ws":
        stream["wsSettings"] = {"path": path, "headers": {"Host": host}}
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": _safe(params.get("serviceName") or params.get("path")),
            "multiMode": str(params.get("mode")) == "multi",
        }
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {"host": host, "path": path}
    elif str(
        params.get("headerType") or (params.get("type") if parsed.protocol == "vmess" else None) or "none"
    ) not in {"none", ""}:
        raise ValueError("unsupported TCP header")
    if parsed.protocol in {"vless", "vmess"}:
        user = {"id": parsed.credential}
        if parsed.protocol == "vless":
            if params.get("encryption", "none") != "none":
                raise ValueError("unsupported VLESS encryption")
            user["encryption"] = "none"
            if params.get("flow"):
                user["flow"] = _safe(params["flow"])
        else:
            user.update(
                {"alterId": int(params.get("aid") or 0), "security": _safe(params.get("scy") or "auto")}
            )
        settings = {"vnext": [{"address": address, "port": parsed.port, "users": [user]}]}
        protocol = parsed.protocol
    elif parsed.protocol == "trojan":
        settings = {"servers": [{"address": address, "port": parsed.port, "password": parsed.credential}]}
        protocol = "trojan"
    else:
        method, credential = parsed.credential.split(":", 1)
        settings = {
            "servers": [{"address": address, "port": parsed.port, "method": method, "password": credential}]
        }
        protocol = "shadowsocks"
    return {
        "log": {"loglevel": "none"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": local_port,
                "protocol": "http",
                "settings": {"accounts": [{"user": username, "pass": password}]},
            }
        ],
        "outbounds": [
            {"tag": "checked-proxy", "protocol": protocol, "settings": settings, "streamSettings": stream}
        ],
    }


def probe_uri(uri: str, settings: Settings) -> ProbeResult:
    if not _PROBE_SLOTS.acquire(timeout=1):
        return ProbeResult(False, "probe_busy")
    try:
        return _probe_uri(uri, settings)
    finally:
        _PROBE_SLOTS.release()


def _probe_uri(uri: str, settings: Settings) -> ProbeResult:
    binary = Path(settings.proxy_probe_binary)
    if not binary.is_file():
        return ProbeResult(False, "core_unavailable")
    parsed = parse_any(uri)
    if parsed is None or validate(parsed):
        return ProbeResult(False, "invalid_config")
    addresses = vetted_addresses(parsed.host, parsed.port)
    if not addresses:
        return ProbeResult(False, "unsafe_or_unresolved_endpoint")
    process = None
    started = time.monotonic()
    deadline = started + min(20.0, max(3.0, settings.proxy_probe_timeout_seconds))
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        username, password = secrets.token_hex(8), secrets.token_hex(16)
        config = build_probe_config(uri, addresses[0], port, username, password)
        with tempfile.TemporaryDirectory(prefix="growth-probe-") as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            path.chmod(0o600)
            # Pinned executable, fixed argv, no shell; URI remains in the private config file.
            process = subprocess.Popen(  # noqa: S603 — fixed argv; pinned executable; shell=False
                [str(binary), "run", "-config", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # noqa: S603
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return ProbeResult(False, "core_rejected_config")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            for target in TARGETS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    with httpx.Client(
                        proxy=f"http://{username}:{password}@127.0.0.1:{port}",
                        timeout=min(remaining, 5.0),
                        trust_env=False,
                        follow_redirects=False,
                    ) as client:
                        with client.stream("GET", target) as response:
                            if response.status_code == 204:
                                return ProbeResult(
                                    True, "https_via_proxy", int((time.monotonic() - started) * 1000)
                                )
                except httpx.HTTPError:
                    continue
        return ProbeResult(False, "https_probe_failed")
    except (ValueError, OSError, TypeError, KeyError):
        return ProbeResult(False, "unsupported_or_invalid_config")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
