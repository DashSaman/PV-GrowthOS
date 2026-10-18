from datetime import datetime, timedelta

from pv_growth.api import webhooks
from pv_growth.database.models import AppConfig, Event
from pv_growth.events.service import get_or_create_user
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def test_direct_buy_enters_supported_purchase_flow_instead_of_home():
    from pv_growth.acquisition.retail_offer import purchase_button

    button = purchase_button()
    assert button["url"] == "https://t.me/pvnetwork_bot?start=buy"
    assert "callback_data" not in button


def test_missing_or_expired_catalogue_never_advertises_a_price(session):
    from pv_growth.acquisition.retail_offer import current_offer

    assert current_offer(session, now=datetime(2026, 10, 18, 13)) is None
    session.add(
        AppConfig(
            key="retail-offer:active",
            value={
                "name": "Personal",
                "volume_gb": 30,
                "duration_days": 30,
                "device_limit": 2,
                "price_toman": 100000,
                "support": "Help in the official bot",
                "source": "owner_confirmed",
                "verified_at_utc": "2026-10-17T12:00:00Z",
                "expires_at_utc": "2026-10-18T12:00:00Z",
            },
        )
    )
    session.flush()
    assert current_offer(session, now=datetime(2026, 10, 18, 13)) is None


def test_current_verified_offer_is_displayed_and_cannot_be_used_past_expiry(session, monkeypatch):
    from pv_growth.acquisition import retail_offer

    now = datetime(2026, 10, 18, 13)
    monkeypatch.setattr(retail_offer, "utcnow", lambda: now)
    session.add(
        AppConfig(
            key="retail-offer:active",
            value={
                "name": "پلن شخصی",
                "volume_gb": 30,
                "duration_days": 30,
                "device_limit": 2,
                "price_toman": 123456,
                "support": "راهنمای اتصال در ربات رسمی",
                "source": "owner_confirmed",
                "verified_at_utc": "2026-10-18T12:00:00Z",
                "expires_at_utc": "2026-10-18T18:00:00Z",
            },
        )
    )
    user, _ = get_or_create_user(session, telegram_user_id=123)
    body, keyboard = retail_offer.record_request(session, user, source="owned", request_id="offer1")
    assert "123,456" in body and "۳۰" in body
    assert "۲ دستگاه" in body
    assert keyboard.buttons[0][0]["url"].endswith("?start=buy")
    assert retail_offer.current_offer(session, now=now + timedelta(hours=6)) is None


def test_payment_status_excludes_old_service_cooperation_wallet_and_unpaid():
    from pv_growth.acquisition.purchase_journey import evaluate_status

    anchor = datetime(2026, 10, 18, 10)
    now = datetime(2026, 10, 18, 13)
    data = {
        "user": {"id": "123", "agent": "f", "User_Status": "Active"},
        "orders": [
            {"Status": "active", "price_product": "100000", "time_sell": "2026-10-17 12:00:00"},
            {"Status": "unpaid", "price_product": "200000", "time_sell": ""},
        ],
        "operations": [],
    }
    result = evaluate_status(data, since=anchor, now=now)
    assert result["status"] == "unpaid_invoice_present" and result["service_toman"] == "0"
    data["orders"].append({"Status": "active", "price_product": "150000", "time_sell": "2026-10-18 16:00:00"})
    assert evaluate_status(data, since=anchor, now=now)["service_toman"] == "150000"
    data["user"]["agent"] = "n2"
    assert evaluate_status(data, since=anchor, now=now)["status"] == "outside_retail_scope"


def test_invalid_or_future_service_history_is_unknown_not_zero():
    from pv_growth.acquisition.purchase_journey import evaluate_status

    data = {
        "user": {"id": "123", "agent": "f", "User_Status": "Active"},
        "orders": [{"Status": "active", "price_product": "100000", "time_sell": "bad"}],
        "operations": [],
    }
    assert (
        evaluate_status(data, since=datetime(2026, 10, 18, 10), now=datetime(2026, 10, 18, 13))["status"]
        == "unavailable"
    )
    data["orders"][0]["time_sell"] = "2026-10-19 10:00:00"
    assert (
        evaluate_status(data, since=datetime(2026, 10, 18, 10), now=datetime(2026, 10, 18, 13))["status"]
        == "unavailable"
    )


def test_status_request_is_private_idempotent_and_cannot_create_payment(session, settings, monkeypatch):
    from pv_growth.acquisition.retail_offer import record_request
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    user, _ = get_or_create_user(session, telegram_user_id=123)
    record_request(session, user, source="web", request_id="start1")
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    calls = []

    def read(self, uid):
        calls.append(uid)
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(MirzaMySQLReader, "fetch_customer_checkout", read)
    transport = FakeTelegramTransport()
    client = TelegramClient(transport, settings)
    cb = webhooks.TgCallback.model_validate(
        {
            "id": "check1",
            "from": {"id": 123},
            "message": {"chat": {"id": 123, "type": "private"}},
            "data": "retail:status",
        }
    )
    webhooks._handle_callback(client, cb)
    webhooks._handle_callback(client, cb)
    assert calls == [123]
    assert "ناموجود" in transport.sent_texts()[-1]
    session.expire_all()
    assert session.query(Event).filter_by(event_type="PAYMENT_SUCCESS").count() == 0
    cb.id = "checkgroup"
    cb.message.chat = {"id": -100, "type": "group"}
    webhooks._handle_callback(client, cb)
    assert calls == [123]


def test_hourly_monitor_is_single_attempt_and_never_dispatches_customer_messages(
    session, settings, monkeypatch
):
    from pv_growth.acquisition import purchase_journey
    from pv_growth.database.models import FreeAllocation, Job, MessageLog
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    now = datetime(2026, 10, 18, 13, 1)
    purchase_journey.enqueue_monitor(session, now)
    purchase_journey.enqueue_monitor(session, now + timedelta(minutes=2))
    session.flush()
    jobs = session.query(Job).filter_by(job_type="retail.monitor").all()
    assert len(jobs) == 1 and jobs[0].max_attempts == 1
    monkeypatch.setattr(purchase_journey, "utcnow", lambda: now)
    monkeypatch.setattr(
        MirzaMySQLReader,
        "fetch_retail_sales_snapshot",
        lambda self: {"users": [], "orders": [], "operations": [], "wallet": []},
    )
    purchase_journey.monitor(session, settings, jobs[0].payload)
    row = session.get(AppConfig, "retail-monitor:hour:2026101813")
    assert row.value["status"] == "ok"
    assert row.value["offer_state"] == "unavailable"
    assert session.query(MessageLog).count() == 0 and session.query(FreeAllocation).count() == 0


def test_customer_checkout_reader_only_reads_requesting_user_and_rolls_back(settings):
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    class Connection:
        def __init__(self):
            self.calls = []
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return self

        def execute(self, query, args=None):
            self.calls.append((query, args))

        def fetchone(self):
            return {"id": "123", "agent": "f", "User_Status": "Active"}

        def fetchall(self):
            return []

        def rollback(self):
            self.rolled_back = True

    conn = Connection()
    reader = MirzaMySQLReader(settings)
    reader.connect = lambda: conn
    result = reader.fetch_customer_checkout(123)
    assert result["orders"] == [] and result["operations"] == []
    assert conn.rolled_back
    assert conn.calls[0] == ("START TRANSACTION READ ONLY", None)
    assert all(q.startswith("SELECT ") and "LIMIT" in q and args == (123,) for q, args in conn.calls[1:])
    assert not any("Payment_report" in q for q, _ in conn.calls)


def test_connection_feedback_stays_self_report_and_never_manufactures_usage(session, settings):
    from pv_growth.acquisition.purchase_journey import action
    from pv_growth.database.models import FreeAllocation, Job

    user, _ = get_or_create_user(session, telegram_user_id=123)
    result = action(session, settings, user, code="not_connected", request_id="feedback1")
    assert "پیش از پرداخت" in result[0]
    assert action(session, settings, user, code="not_connected", request_id="feedback1") is None
    assert session.query(Event).filter_by(event_type="RETAIL_CONNECTION_REPORTED").count() == 1
    assert session.query(Event).filter_by(event_type="TRIAL_CONNECTED").count() == 0
    assert session.query(FreeAllocation).count() == 0 and session.query(Job).count() == 0


def test_reopening_guide_does_not_hide_purchase_after_original_request(session, settings, monkeypatch):
    from pv_growth.acquisition import purchase_journey
    from pv_growth.events.service import ingest
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    user, _ = get_or_create_user(session, telegram_user_id=123)
    for hour in [10, 12]:
        ingest(
            session,
            "RETAIL_OFFER_REQUESTED",
            user_id=user.id,
            occurred_at=datetime(2026, 10, 18, hour),
            idempotency_key=f"request:{hour}",
            metadata={"source": "owned"},
        )
    session.commit()
    monkeypatch.setattr(purchase_journey, "utcnow", lambda: datetime(2026, 10, 18, 13))
    monkeypatch.setattr(
        MirzaMySQLReader,
        "fetch_customer_checkout",
        lambda self, uid: {
            "user": {"id": "123", "agent": "f", "User_Status": "Active"},
            "orders": [{"Status": "active", "price_product": "150000", "time_sell": "2026-10-18 14:30:00"}],
            "operations": [],
        },
    )
    body, _ = purchase_journey.action(session, settings, user, code="status", request_id="checkpaid")
    assert "150000" in body
    assert "✅" in body
