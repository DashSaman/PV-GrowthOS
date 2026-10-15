import importlib.util
import json
from datetime import UTC, datetime

import pytest


def module():
    assert importlib.util.find_spec("pv_growth.mirza_adapter.retail_sales"), (
        "segmented business-date sales report missing"
    )
    from pv_growth.mirza_adapter import retail_sales

    return retail_sales


def stamp(day, hour=12):
    return str(int(datetime(2026, 10, day, hour, tzinfo=UTC).timestamp()))


def snapshot():
    return {
        "users": [{"id": 71, "agent": "f"}, {"id": 82, "agent": "n2"}],
        "orders": [
            {"id_user": 71, "price_product": "100,000", "Status": "active", "time_sell": stamp(15)},
            {"id_user": 82, "price_product": "800000", "Status": "disabledn", "time_sell": stamp(15)},
            {"id_user": 71, "price_product": "0", "Status": "active", "time_sell": stamp(15)},
            {"id_user": 71, "price_product": "500000", "Status": "unpaid", "time_sell": stamp(15)},
            {"id_user": 71, "price_product": "200000", "Status": "active", "time_sell": stamp(14)},
            {"id_user": 71, "price_product": "999999", "Status": "active", "time_sell": stamp(16, 0)},
        ],
        "operations": [
            {
                "id_user": 71,
                "type": "extend_user",
                "status": "paid",
                "price": "50000",
                "time": "2026-10-15 16:00:00",
            }
        ],
        "wallet": [{"id_user": 71, "payment_Status": "paid", "price": "100000", "time": stamp(15)}],
    }


def test_role_split_uses_business_dates_excludes_free_and_wallet_double_counting():
    report = module().summarize(
        snapshot(), start=datetime(2026, 10, 15, tzinfo=UTC), end=datetime(2026, 10, 16, tzinfo=UTC)
    )
    retail = report["current"]["segments"]["ordinary"]
    assert retail["recorded_service_toman"] == "150000"
    assert retail["service_buyers"] == 1 and retail["orders"] == 1 and retail["operations"] == 1
    assert retail["wallet_toman_separate"] == "100000"
    assert report["previous"]["segments"]["ordinary"]["recorded_service_toman"] == "200000"
    assert report["current"]["segments"]["cooperation"]["recorded_service_toman"] == "800000"
    assert report["previous"]["end_utc"] == report["current"]["start_utc"]
    assert report["current"]["end_utc"] == "2026-10-16T00:00:00+00:00"
    assert "71" not in json.dumps(report) and "82" not in json.dumps(report)


def test_cohort_purchase_requires_actual_sale_after_confirmed_delivery_and_keeps_unknown_roles():
    data = snapshot()
    data["orders"].append({"id_user": 99, "price_product": "13", "Status": "active", "time_sell": stamp(15)})
    report = module().summarize(
        data,
        start=datetime(2026, 10, 15, tzinfo=UTC),
        end=datetime(2026, 10, 16, tzinfo=UTC),
        recipients={"71": datetime(2026, 10, 15, 13, tzinfo=UTC)},
    )
    # 12:00 order and 12:30 renewal precede the 13:00 delivery.
    assert report["intro"]["buyers_after_delivery"] == 0
    assert report["current"]["segments"]["unknown"]["recorded_service_toman"] == "13"
    data["orders"][0]["time_sell"] = stamp(15, 14)
    report = module().summarize(
        data,
        start=datetime(2026, 10, 15, tzinfo=UTC),
        end=datetime(2026, 10, 16, tzinfo=UTC),
        recipients={"71": datetime(2026, 10, 15, 13, tzinfo=UTC)},
    )
    assert report["intro"]["buyers_after_delivery"] == 1
    assert report["intro"]["recorded_service_toman"] == "100000"


def test_malformed_amount_or_date_is_visible_and_not_a_fabricated_zero_success():
    data = snapshot()
    data["orders"][0]["time_sell"] = "bad"
    data["operations"][0]["price"] = "NaN"
    report = module().summarize(
        data, start=datetime(2026, 10, 15, tzinfo=UTC), end=datetime(2026, 10, 16, tzinfo=UTC)
    )
    assert report["quality"]["orders_invalid_time"] == 1
    assert report["quality"]["operations_invalid_amount"] == 1
    assert report["data_complete"] is False
    assert "ناقص" in module().render_report(report)


def test_report_reader_is_readonly_bounded_and_rolls_back(settings):
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    reader = MirzaMySQLReader(settings)
    assert hasattr(reader, "fetch_retail_sales_snapshot"), "bounded reporting reader missing"

    class Fake:
        def __init__(self):
            self.queries = []
            self.rolled = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def cursor(self):
            return self

        def execute(self, q, *a):
            self.queries.append(q)

        def fetchall(self):
            return []

        def rollback(self):
            self.rolled = True

    conn = Fake()
    reader.connect = lambda: conn
    assert reader.fetch_retail_sales_snapshot() == {"users": [], "orders": [], "operations": [], "wallet": []}
    assert conn.queries[0] == "START TRANSACTION READ ONLY" and conn.rolled
    assert all("LIMIT" in q for q in conn.queries[1:])
    assert all(q.startswith(("START TRANSACTION READ ONLY", "SELECT ")) for q in conn.queries)


def test_native_report_is_saved_even_without_registered_manager(session, settings, monkeypatch):
    from pv_growth.database.models import AppConfig, MessageLog
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
    from pv_growth.telegram import management

    monkeypatch.setattr(management, "utcnow", lambda: datetime(2026, 10, 15, 20, 25))
    calls = []

    def read(self):
        calls.append(1)
        return snapshot()

    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", read, raising=False)
    management.daily_report(session, settings, {"day": "2026-10-15"})
    row = session.get(AppConfig, "retail-sales:daily:2026-10-15")
    assert row is not None, "server must retain daily sales aggregate without owner/Codex"
    assert row.value["status"] == "ok" and row.value["current"]["end_utc"] == "2026-10-15T20:25:00+00:00"
    management.daily_report(session, settings, {"day": "2026-10-15"})
    assert len(calls) == 1 and session.query(MessageLog).count() == 0


def test_unavailable_mirza_is_explicit_not_zero_and_contains_no_secret(session, settings, monkeypatch):
    m = module()
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    def fail(self):
        raise RuntimeError("secret DSN must never be stored")

    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", fail)
    r = m.collect_report(
        session, settings, key="retail-sales:test", end=datetime(2026, 10, 15, 20, 25, tzinfo=UTC)
    )
    assert r["status"] == "unavailable" and "current" not in r
    assert "secret" not in json.dumps(r)
    assert "ناموجود" in m.render_report(r)


def test_invalid_report_window_rejected():
    m = module()
    with pytest.raises(ValueError):
        m.summarize(
            snapshot(), start=datetime(2026, 10, 16, tzinfo=UTC), end=datetime(2026, 10, 15, tzinfo=UTC)
        )


def test_sales_command_requires_channel_owner_and_keeps_report_private(session, settings, monkeypatch):
    from pv_growth.api import webhooks
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
    from pv_growth.telegram import management
    from tests.test_server_autonomy import ManagerClient

    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", lambda self: snapshot())
    monkeypatch.setattr(management, "utcnow", lambda: datetime(2026, 10, 15, 17, 15))
    client = ManagerClient(False)
    message = webhooks.TgMessage.model_validate(
        {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/sales"}
    )
    webhooks._handle_message(client, message)
    assert not any("فروش ثبت‌شده" in text for text in client.sent)
    client.owner = True
    webhooks._handle_message(client, message)
    assert "فروش ثبت‌شده" in client.sent[-1]
    assert "عادی" in client.sent[-1] and "همکاری" in client.sent[-1]
    assert len(client.sent[-1]) < 4096


def test_over_bound_reader_fails_instead_of_returning_partial_sales(settings):
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return self

        def execute(self, *args):
            pass

        def fetchall(self):
            return [{}] * 10001

        def rollback(self):
            self.rolled = True

    conn = Fake()
    reader = MirzaMySQLReader(settings)
    reader.connect = lambda: conn
    with pytest.raises(ValueError, match="bound"):
        reader.fetch_retail_sales_snapshot()
    assert conn.rolled
