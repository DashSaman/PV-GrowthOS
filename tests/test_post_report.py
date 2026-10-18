from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from pv_growth.database.models import AppConfig, Event, MessageLog
from pv_growth.events.service import get_or_create_user

NOW = datetime(2026, 10, 19, 12, 5)
POST = "pvnetwork0/308"


def config():
    return {
        "status": "active",
        "post_message_id": 308,
        "post_url": "https://t.me/pvnetwork0/308",
        "published_at_utc": "2026-10-18T10:00:00+00:00",
        "tracking_from_utc": "2026-10-18T11:00:00+00:00",
        "attribution_enabled": True,
    }


def classification():
    return {
        "source": "owner_confirmed",
        "all_time_test_account_keys": [sha256(b"mirza-user:102").hexdigest()],
        "ordinary_test_share_estimate_percent": 50,
    }


def stamp(hour, day=19):
    return str(int(datetime(2026, 10, day, hour, tzinfo=UTC).timestamp()))


def data():
    return {
        "users": [{"id": str(i), "agent": "n2" if i == 103 else "f"} for i in range(101, 107)],
        "orders": [
            {
                "id_invoice": "invoice-" + str(i),
                "id_user": str(i),
                "Status": "active",
                "price_product": amount,
                "time_sell": stamp(hour),
            }
            for i, amount, hour in [
                (101, "100000", 11),
                (102, "999990", 11),
                (103, "400000", 11),
                (104, "200000", 9),
                (105, "500000", 11),
                (106, "600000", 11),
            ]
        ],
        "operations": [
            {"id_user": "101", "type": "extend_user", "status": "paid", "price": "30000", "time": stamp(11)}
        ],
        "wallet": [{"id_user": "101", "payment_Status": "paid", "price": "50000", "time": stamp(11)}],
    }


def touches():
    return {str(i): [datetime(2026, 10, 19, 10, tzinfo=UTC)] for i in range(101, 105)} | {
        "106": [datetime(2026, 10, 10, 10, tzinfo=UTC)]
    }


def test_post_matching_excludes_all_test_orders_cooperation_wallet_and_pre_entry_sales():
    from pv_growth.acquisition.post_report import summarize_post

    report = summarize_post(
        data(),
        start=datetime(2026, 10, 18, 10, tzinfo=UTC),
        end=NOW.replace(tzinfo=UTC),
        entries=touches(),
        classification=classification(),
    )
    assert report["matched"]["service_buyers"] == 1
    assert report["matched"]["service_records"] == 2
    assert report["matched"]["recorded_service_toman"] == "130000"
    assert report["ordinary_after_test_exclusions"]["recorded_service_toman"] == "1430000"
    assert report["confirmed_internal_test_excluded"]["recorded_service_toman"] == "999990"
    assert report["causal_incremental_revenue_proven"] is False
    assert "invoice-101" not in str(report)
    assert report["matched_orders"][0]["purchase_key"]


def test_unknown_classification_or_corrupt_paid_timestamp_never_becomes_zero():
    from pv_growth.acquisition.post_report import summarize_post

    with pytest.raises(ValueError):
        summarize_post(
            data(),
            start=datetime(2026, 10, 18, tzinfo=UTC),
            end=NOW.replace(tzinfo=UTC),
            entries=touches(),
            classification=None,
        )
    broken = data()
    broken["orders"][0]["time_sell"] = "bad"
    with pytest.raises(ValueError):
        summarize_post(
            broken,
            start=datetime(2026, 10, 18, tzinfo=UTC),
            end=NOW.replace(tzinfo=UTC),
            entries=touches(),
            classification=classification(),
        )


def test_post_start_reserves_one_entry_without_any_purchase_or_grant(session, monkeypatch):
    from pv_growth.acquisition import post_report

    monkeypatch.setattr(post_report, "utcnow", lambda: NOW)
    session.add(AppConfig(key=post_report.CONFIG_KEY, value=config()))
    user, _ = get_or_create_user(session, telegram_user_id=101)
    session.commit()
    result = post_report.record_entry(session, user, request_id="m:42")
    assert result[1].buttons[0][0]["url"] == "https://t.me/pvnetwork_bot?start=buy"
    assert post_report.record_entry(session, user, request_id="m:42") is None
    events = session.query(Event).all()
    assert len(events) == 1 and events[0].event_type == "RETAIL_POST_ENTERED"
    assert events[0].metadata_json["post"] == POST
    assert session.query(MessageLog).count() == 0


def test_private_post_link_routes_before_free_claim_checks(session, settings, monkeypatch):
    from pv_growth.acquisition import post_report
    from pv_growth.api import webhooks
    from pv_growth.free_config import audience
    from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

    monkeypatch.setattr(post_report, "utcnow", lambda: NOW)
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    monkeypatch.setattr(audience, "decision_for_user", lambda *a: pytest.fail("purchase entry cannot claim"))
    session.add(AppConfig(key=post_report.CONFIG_KEY, value=config()))
    session.commit()
    transport = FakeTelegramTransport()
    client = TelegramClient(transport, settings)
    message = webhooks.TgMessage.model_validate(
        {
            "message_id": 9,
            "from": {"id": 101},
            "chat": {"id": 101, "type": "private"},
            "text": "/start post_pv0_308",
        }
    )
    webhooks._handle_message(client, message)
    session.expire_all()
    assert session.query(Event).filter_by(event_type="RETAIL_POST_ENTERED").count() == 1
    assert transport.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("?start=buy")
    message.chat = {"id": -123, "type": "group"}
    webhooks._handle_message(client, message)
    assert session.query(Event).filter_by(event_type="RETAIL_POST_ENTERED").count() == 1


def test_hourly_native_report_saves_fixed_milestone_and_never_sends(session, settings, monkeypatch):
    from pv_growth.acquisition import post_report
    from pv_growth.database.models import Job
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    monkeypatch.setattr(post_report, "utcnow", lambda: NOW)
    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", lambda self: data())
    monkeypatch.setattr(post_report, "read_views", lambda: {"status": "ok", "displayed_views": "123"})
    session.add_all(
        [
            AppConfig(key=post_report.CONFIG_KEY, value=config()),
            AppConfig(key="retail-sales:classification", value=classification()),
        ]
    )
    session.commit()
    post_report.enqueue_monitor(session, NOW)
    post_report.enqueue_monitor(session, NOW + timedelta(minutes=2))
    session.flush()
    jobs = session.query(Job).filter_by(job_type="retail.post_report").all()
    assert len(jobs) == 1 and jobs[0].max_attempts == 1
    post_report.monitor(session, settings, jobs[0].payload)
    report = session.get(AppConfig, "retail-post:latest").value
    assert report["status"] == "ok" and report["views"]["displayed_views"] == "123"
    assert report["matched"]["service_buyers"] == 0
    milestone = session.get(AppConfig, "retail-post:milestone:24").value
    assert milestone["end_utc"] == "2026-10-19T10:00:00+00:00"
    assert session.get(AppConfig, "retail-post:milestone:72") is None
    post_report.monitor(session, settings, jobs[0].payload)
    assert session.query(MessageLog).count() == 0
    assert session.query(Event).count() == 0


def test_native_failure_is_saved_as_unavailable_not_zero_sales(session, settings, monkeypatch):
    from pv_growth.acquisition import post_report
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    monkeypatch.setattr(post_report, "utcnow", lambda: NOW)

    def fail(self):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", fail)
    monkeypatch.setattr(post_report, "read_views", lambda: {"status": "unavailable"})
    session.add_all(
        [
            AppConfig(key=post_report.CONFIG_KEY, value=config()),
            AppConfig(key="retail-sales:classification", value=classification()),
        ]
    )
    session.commit()
    post_report.monitor(session, settings, {"hour": "2026101912"})
    report = session.get(AppConfig, "retail-post:latest").value
    assert report["status"] == "unavailable" and "matched" not in report
    assert session.get(AppConfig, "retail-post:milestone:24") is None
