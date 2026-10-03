"""Read-only Mirza adapter.

Mirza is the source of truth for orders/payments/provisioning/renewal.
GrowthOS never writes to Mirza and never duplicates its logic. Preferred
order (spec §11): existing API → read-only adapter. HTTP reads only.

Enable by setting PVG_MIRZA_BASE_URL + PVG_MIRZA_TOKEN. Without them the
client raises NotConfigured and callers degrade gracefully (BLOCKERS.md B4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from pv_growth.core.config import Settings
from pv_growth.core.errors import ExternalServiceError, NotConfigured
from pv_growth.core.logging import get_logger

log = get_logger("mirza")


@dataclass(frozen=True)
class MirzaOrder:
    order_id: str
    user_ref: str
    status: str
    amount_cents: int
    currency: str
    plan_code: str | None
    paid_at: datetime | None
    created_at: datetime | None
    service_id: str | None


@dataclass(frozen=True)
class MirzaService:
    service_id: str
    user_ref: str
    status: str
    expires_at: datetime | None
    traffic_used_mb: int | None
    traffic_limit_mb: int | None


class MirzaClient(Protocol):
    def get_order(self, order_id: str) -> MirzaOrder: ...
    def list_orders(self, since: datetime | None = None, limit: int = 50) -> list[MirzaOrder]: ...
    def get_user_orders(self, user_ref: str) -> list[MirzaOrder]: ...
    def get_service(self, service_id: str) -> MirzaService: ...


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _order_from_json(data: dict) -> MirzaOrder:
    return MirzaOrder(
        order_id=str(data["id"]),
        user_ref=str(data.get("user_ref", "")),
        status=str(data.get("status", "unknown")),
        amount_cents=int(data.get("amount_cents", 0)),
        currency=str(data.get("currency", "IRR")),
        plan_code=data.get("plan_code"),
        paid_at=_parse_ts(data.get("paid_at")),
        created_at=_parse_ts(data.get("created_at")),
        service_id=data.get("service_id"),
    )


def _service_from_json(data: dict) -> MirzaService:
    return MirzaService(
        service_id=str(data["id"]),
        user_ref=str(data.get("user_ref", "")),
        status=str(data.get("status", "unknown")),
        expires_at=_parse_ts(data.get("expires_at")),
        traffic_used_mb=data.get("traffic_used_mb"),
        traffic_limit_mb=data.get("traffic_limit_mb"),
    )


class HttpMirzaClient:
    """Thin read-only HTTP adapter. Adjust `_ROUTES` when Mirza's actual
    endpoints are confirmed on the server; the interface stays stable."""

    _ROUTES = {
        "order": "/api/orders/{id}",
        "orders": "/api/orders",
        "user_orders": "/api/users/{ref}/orders",
        "service": "/api/services/{id}",
    }

    def __init__(self, settings: Settings, transport: httpx.Client | None = None) -> None:
        if not settings.mirza_base_url or not settings.mirza_token:
            raise NotConfigured("Mirza base URL/token not configured (PVG_MIRZA_*)")
        self._settings = settings
        self._client = transport or httpx.Client(
            base_url=settings.mirza_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.mirza_token}"},
            timeout=settings.mirza_timeout_seconds,
        )

    def _get(self, route: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.get(route, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            log.error("mirza read failed", route=route, error=str(exc))
            raise ExternalServiceError(f"mirza: {exc}") from exc

    def get_order(self, order_id: str) -> MirzaOrder:
        data = self._get(self._ROUTES["order"].format(id=order_id)).json()
        return _order_from_json(data)

    def list_orders(self, since: datetime | None = None, limit: int = 50) -> list[MirzaOrder]:
        params: dict = {"limit": limit}
        if since:
            params["since"] = since.isoformat()
        data = self._get(self._ROUTES["orders"], params=params).json()
        items = data if isinstance(data, list) else data.get("items", [])
        return [_order_from_json(item) for item in items]

    def get_user_orders(self, user_ref: str) -> list[MirzaOrder]:
        data = self._get(self._ROUTES["user_orders"].format(ref=user_ref)).json()
        items = data if isinstance(data, list) else data.get("items", [])
        return [_order_from_json(item) for item in items]

    def get_service(self, service_id: str) -> MirzaService:
        data = self._get(self._ROUTES["service"].format(id=service_id)).json()
        return _service_from_json(data)


class FakeMirza:
    """In-memory Mirza for tests — proves the adapter contract without a server."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.services: dict[str, dict] = {}

    def add_order(self, **kwargs) -> dict:
        self.orders[str(kwargs["id"])] = kwargs
        return kwargs

    def add_service(self, **kwargs) -> dict:
        self.services[str(kwargs["id"])] = kwargs
        return kwargs

    def get_order(self, order_id: str) -> MirzaOrder:
        return _order_from_json(self.orders[order_id])

    def list_orders(self, since: datetime | None = None, limit: int = 50) -> list[MirzaOrder]:
        result = [_order_from_json(o) for o in self.orders.values()]
        if since:
            result = [
                o for o in result
                if (o.paid_at or o.created_at) and (o.paid_at or o.created_at) >= since
            ]
        return result[:limit]

    def get_user_orders(self, user_ref: str) -> list[MirzaOrder]:
        return [_order_from_json(o) for o in self.orders.values() if o.get("user_ref") == user_ref]

    def get_service(self, service_id: str) -> MirzaService:
        return _service_from_json(self.services[service_id])


def get_mirza(settings: Settings) -> MirzaClient:
    """Factory used by services; returns HttpMirzaClient or raises NotConfigured."""
    return HttpMirzaClient(settings)
