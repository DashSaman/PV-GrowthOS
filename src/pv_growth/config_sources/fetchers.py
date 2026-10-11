"""Public source fetchers: GitHub raw files and Telegram channel previews.

Only admin-registered sources are fetched. Telegram uses the public web
preview (t.me/s/<channel>) — lightweight HTTP, no client API, no crawling.
"""

from __future__ import annotations

import re

import httpx

from pv_growth.core.logging import get_logger

log = get_logger("fetchers")

_URI_RE = re.compile(r"(vmess|vless|trojan|ss)://[A-Za-z0-9+/\-_.:=@?&%#]+", re.IGNORECASE)


def extract_config_uris(text: str) -> list[str]:
    """Pull every proxy URI out of arbitrary text (subscription file, HTML)."""
    return [m.group(0).rstrip(".,;)\"'") for m in _URI_RE.finditer(text)]


def fetch_github_file(url: str, client: httpx.Client) -> list[str]:
    return extract_config_uris(_bounded_text(client, url))


def _bounded_text(client: httpx.Client, url: str, **kwargs) -> str:
    with client.stream("GET", url, **kwargs) as resp:
        resp.raise_for_status()
        body = bytearray()
        for chunk in resp.iter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > 1048576:
                raise ValueError("source too large")
            body.extend(chunk)
        return body.decode(resp.encoding or "utf-8", errors="replace")


def fetch_telegram_preview(channel: str, client: httpx.Client) -> list[str]:
    handle = channel.lstrip("@")
    return extract_config_uris(
        _bounded_text(client, f"https://t.me/s/{handle}", headers={"User-Agent": "Mozilla/5.0"})
    )


def fetch_source(kind: str, url: str, client: httpx.Client) -> list[str]:
    if kind == "github_file":
        return fetch_github_file(url, client)
    if kind == "telegram_channel":
        return fetch_telegram_preview(url, client)
    raise ValueError(f"unknown source kind: {kind}")
