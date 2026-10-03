"""Phase 1 acceptance: read-only Mirza adapter contract (fake-backed) + HTTP client."""

import httpx
import pytest

from pv_growth.core.config import Settings
from pv_growth.core.errors import ExternalServiceError, NotConfigured
from pv_growth.mirza_adapter.client import FakeMirza, HttpMirzaClient


def test_fake_mirza_contract():
    fake = FakeMirza()
    fake.add_order(id="o1", user_ref="u1", status="paid", amount_cents=100000,
                   paid_at="2026-10-01T10:00:00Z")
    order = fake.get_order("o1")
    assert order.order_id == "o1"
    assert order.status == "paid"
    assert order.paid_at is not None
    assert fake.get_user_orders("u1")[0].order_id == "o1"
    assert fake.get_user_orders("nobody") == []


def test_http_mirza_not_configured():
    with pytest.raises(NotConfigured):
        HttpMirzaClient(Settings(database_url="sqlite+pysqlite:///./x.db"))


def test_http_mirza_reads_only(monkeypatch):
    settings = Settings(
        database_url="sqlite+pysqlite:///./x.db",
        mirza_base_url="https://mirza.example",
        mirza_token="tok",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/orders/o1":
            return httpx.Response(200, json={
                "id": "o1", "user_ref": "u1", "status": "paid", "amount_cents": 5,
                "paid_at": "2026-10-01T10:00:00Z",
            })
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = HttpMirzaClient(settings, transport=httpx.Client(transport=transport,
                                                             base_url="https://mirza.example"))
    order = client.get_order("o1")
    assert order.amount_cents == 5
    assert all(r.method == "GET" for r in requests)  # read-only guarantee

    with pytest.raises(ExternalServiceError):
        client.get_order("missing")  # 404 -> wrapped, never crashes caller
