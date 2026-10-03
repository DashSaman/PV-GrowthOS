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
    resp = client.get(url)
    resp.raise_for_status()
    return extract_config_uris(resp.text)


def fetch_telegram_preview(channel: str, client: httpx.Client) -> list[str]:
    handle = channel.lstrip("@")
    resp = client.get(f"https://t.me/s/{handle}", headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return extract_config_uris(resp.text)


def fetch_source(kind: str, url: str, client: httpx.Client) -> list[str]:
    if kind == "github_file":
        return fetch_github_file(url, client)
    if kind == "telegram_channel":
        return fetch_telegram_preview(url, client)
    raise ValueError(f"unknown source kind: {kind}")
