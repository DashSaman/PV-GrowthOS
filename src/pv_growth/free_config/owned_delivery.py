"""Fetch only the configured owned subscription; deliver protocol proof."""

from __future__ import annotations

import base64
import hmac
import re
from urllib.parse import urlsplit

import httpx

from pv_growth.config_quality.parsers import parse_any
from pv_growth.config_quality.probe import probe_uri
from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError


def require_client_identity(service: dict, state: dict) -> None:
    for key in ("client_id", "sub_id"):
        value = service.get(key)
        if not isinstance(value, str) or not value or state.get(key) != value:
            raise ValidationError("authoritative panel client identity mismatch")


def verified_uri(service: dict, settings: Settings, *, transport=None) -> tuple[str, int | None]:
    origin = urlsplit(settings.provisioning_sublink.rstrip("/"))
    raw_url = service.get("config_uri") or service.get("subscription_url") or ""
    url = urlsplit(raw_url)
    prefix = origin.path.rstrip("/") + "/"
    token = url.path[len(prefix) :] if url.path.startswith(prefix) else ""
    if (
        origin.scheme != "https"
        or url.scheme != "https"
        or url.netloc != origin.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token)
        or token != service.get("sub_id")
        or not isinstance(service.get("client_id"), str)
        or not service["client_id"]
    ):
        raise ValidationError("owned subscription origin mismatch")
    try:
        with httpx.Client(
            timeout=8.0, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            with client.stream("GET", raw_url, headers={"User-Agent": "v2rayNG/1.8"}) as response:
                if response.status_code != 200:
                    raise ValidationError("owned subscription unavailable")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise ValidationError("owned subscription too large")
        text = content.decode("utf-8", errors="strict").strip()
        if "://" not in text:
            text = base64.b64decode(text + "=" * (-len(text) % 4), validate=True).decode("utf-8")
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        raise ValidationError("owned subscription unreadable") from exc
    candidates = [
        line.strip()
        for line in text.splitlines()
        if len(line) <= 16384 and parse_any(line.strip()) is not None
    ][:3]
    for uri in candidates:
        parsed = parse_any(uri)
        if parsed.protocol != "vless" or not hmac.compare_digest(parsed.credential, service["client_id"]):
            continue
        result = probe_uri(uri, settings)
        if result.ok:
            return uri, result.latency_ms
    raise ValidationError("owned config actual HTTPS check failed")
