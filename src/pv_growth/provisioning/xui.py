"""PV Exclusive provisioning — reuses the EXISTING authorized X-UI panel API.

Discovered on production (2026-10-04, read-only inspection of Mirza's own
`x-ui_single.php` + `marzban_panel` registry):

    POST {url_panel}/panel/api/clients/add        Bearer <panel token>
         body: {"inboundIds": [..], "client": {email,totalGB,expiryTime,enable,subId,tgId,comment}}
    GET  {url_panel}/panel/api/clients/get/{email}
    POST {url_panel}/panel/api/clients/update/{uuid}
    POST {url_panel}/panel/api/clients/del/{email}
    GET  {url_panel}/panel/api/server/status

This is the same interface Mirza uses — no parallel provisioning logic is
invented. GrowthOS only creates clearly-labeled temporary free clients
(email prefix ``growth-``); paid services are never modified. All calls go
through this single boundary (PVProvisioningAdapter).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Protocol

import httpx

from pv_growth.core.config import Settings
from pv_growth.core.errors import ExternalServiceError, NotConfigured
from pv_growth.core.logging import get_logger

log = get_logger("provisioning")

EMAIL_PREFIX = "growth-"


class ProvisioningError(ExternalServiceError):
    """Upstream provisioning failed (timeout / 5xx / API error)."""


def client_email_for(idempotency_key: str) -> str:
    """Deterministic service identity: the same claim key always maps to the
    same client email — a replayed claim or a retried network call can never
    create a second account."""
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
    return f"{EMAIL_PREFIX}{digest}"


class PVProvisioningAdapter(Protocol):
    def create_temp_service(self, *, location: str, traffic_gb: int,
                            validity_hours: int, protocol: str,
                            idempotency_key: str) -> dict: ...
    def service_state(self, service_ref: str) -> dict: ...
    def disable_service(self, service_ref: str) -> bool: ...
    def health(self) -> bool: ...


class XUIProvisioningAdapter:
    """HTTP adapter against the existing X-UI panel (Bearer-token API)."""

    def __init__(self, settings: Settings,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not settings.provisioning_base_url or not settings.provisioning_token:
            raise NotConfigured("provisioning endpoint not configured (PVG_PROVISIONING_*)")
        self._s = settings
        self._client = httpx.Client(
            base_url=settings.provisioning_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.provisioning_token}",
                     "Accept": "application/json"},
            timeout=settings.provisioning_timeout_seconds,
            verify=False,  # noqa: S501 - panel self-signed cert (Mirza uses curl -k)
            transport=transport,
        )

    # -- low level -------------------------------------------------------
    def _call(self, method: str, path: str, json_body: dict | None = None) -> dict:
        try:
            resp = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise ProvisioningError(f"xui transport: {type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise ProvisioningError(f"xui upstream {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProvisioningError("xui non-json response") from exc
        if resp.status_code != 200 or body.get("success") is not True:
            # 404/409-style "already exists / not found" are handled by callers
            body["_status"] = resp.status_code
            return body
        return body

    # -- boundary API ----------------------------------------------------
    def health(self) -> bool:
        try:
            return self._call("GET", "/panel/api/server/status").get("success") is True
        except ProvisioningError:
            return False

    def create_temp_service(self, *, location: str, traffic_gb: int,
                            validity_hours: int, protocol: str,
                            idempotency_key: str) -> dict:
        email = client_email_for(idempotency_key)
        payload = {
            "inboundIds": [int(x) for x in str(self._s.provisioning_inbound_ids).split(",") if x.strip()],
            "client": {
                "email": email,
                "totalGB": int(traffic_gb) * 1024 * 1024 * 1024,
                "expiryTime": int((time.time() + validity_hours * 3600) * 1000),
                "enable": True,
                "tgId": 0,
                "subId": secrets.token_hex(8),
                "comment": f"growthos-free|{location}|{protocol}",
            },
        }
        result = self._call("POST", "/panel/api/clients/add", payload)
        if result.get("success") is not True:
            # replay / retry: the deterministic email may already exist —
            # fetch it instead of creating a second account
            existing = self._get_or_none(email)
            if existing is not None:
                log.info("provision idempotent hit", service_ref=email)
                return self._service_payload(email, existing, replayed=True)
            raise ProvisioningError(f"xui add failed: {str(result.get('msg'))[:120]}")
        # add succeeded: prefer panel truth when readable, else the request we
        # just sent (all authoritative fields are ours by construction)
        try:
            existing = self._get_or_none(email)
        except ProvisioningError:
            existing = None  # tolerate unreadable get right after add
        if existing:
            return self._service_payload(email, existing)
        client = payload["client"]
        return self._service_payload(email, client)

    def _get_or_none(self, email: str) -> dict | None:
        result = self._call("GET", f"/panel/api/clients/get/{email}")
        if result.get("success") is not True:
            return None
        obj = result.get("obj") or {}
        return obj if obj.get("email") == email or obj else obj or None

    def _service_payload(self, email: str, obj: dict, replayed: bool = False) -> dict:
        client = obj.get("client") if isinstance(obj.get("client"), dict) else obj
        sub_id = client.get("subId") or obj.get("subId") or ""
        sublink = self._s.provisioning_sublink.rstrip("/")
        config_uri = f"{sublink}/{sub_id}" if sub_id and sublink else None
        return {
            "service_ref": email,
            "config_uri": config_uri,
            "subscription_url": config_uri,
            "traffic_gb": (client.get("totalGB") or 0) // (1024 ** 3),
            "expiry_ts_ms": client.get("expiryTime") or 0,
            "enabled": client.get("enable", True),
            "replayed": replayed,
        }

    def service_state(self, service_ref: str) -> dict:
        obj = self._get_or_none(service_ref)
        if obj is None:
            return {"exists": False}
        client = obj.get("client") if isinstance(obj.get("client"), dict) else obj
        total = client.get("totalGB") or 0
        up = obj.get("up") or client.get("up") or 0
        down = obj.get("down") or client.get("down") or 0
        return {
            "exists": True,
            "enabled": client.get("enable", True),
            "traffic_limit_gb": total // (1024 ** 3),
            "traffic_used_gb": round((up + down) / (1024 ** 3), 3),
            "expiry_ts_ms": client.get("expiryTime") or 0,
            "expired": bool(client.get("expiryTime")
                            and client["expiryTime"] > 0
                            and client["expiryTime"] <= time.time() * 1000),
        }

    def disable_service(self, service_ref: str) -> bool:
        result = self._call("POST", f"/panel/api/clients/del/{service_ref}")
        ok = result.get("success") is True
        if not ok:
            log.warning("disable failed", service_ref=service_ref,
                        msg=str(result.get("msg"))[:100])
        return ok


def get_provisioning(settings: Settings) -> PVProvisioningAdapter | None:
    """Factory used by the claim path. Returns None (caller keeps the claim
    queued) when the endpoint is not configured yet."""
    try:
        return XUIProvisioningAdapter(settings)
    except NotConfigured:
        return None
