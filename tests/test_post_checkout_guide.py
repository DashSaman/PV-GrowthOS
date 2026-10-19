from datetime import UTC, datetime, timedelta

import pytest

from pv_growth.database.models import AppConfig, Event, FreeAllocation, Job, MessageLog
from pv_growth.events.service import get_or_create_user, ingest

NOW = datetime(2026, 10, 19, 14)


def catalogue(observed=NOW):
    return {
        "source": "mirza_local_readonly",
        "observed_at_utc": observed.replace(tzinfo=UTC).isoformat(),
        "plans": [
            {
                "code": "small",
                "name": "fixture-small",
                "location": "fixture",
                "volume_gb": 10,
                "duration_days": 31,
                "price_toman": 50000,
            },
            {
                "code": "personal",
                "name": "fixture-personal",
                "location": "fixture",
                "volume_gb": 20,
                "duration_days": 31,
                "price_toman": 100000,
            },
        ],
        "payment": {
            "direct_buy": False,
            "minimum_wallet_toman": 100000,
            "card_to_support": True,
            "support_handle": "FixtureSupport",
        },
    }


def entry_config():
    return {
        "status": "active",
        "post_message_id": 308,
        "post_url": "https://t.me/pvnetwork0/308",
        "published_at_utc": "2026-10-18T16:29:13+00:00",
        "tracking_from_utc": "2026-10-18T17:25:26+00:00",
        "attribution_enabled": True,
    }


@pytest.mark.parametrize("stale", [False, True])
def test_post_start_has_a_usable_checkout_guide_and_never_uses_stale_price(
    session, settings, monkeypatch, stale
):
    from pv_growth.acquisition import post_report
    from pv_growth.api import webhooks
    from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

    monkeypatch.setattr(post_report, "utcnow", lambda: NOW)
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    session.add_all(
        [
            AppConfig(key=post_report.CONFIG_KEY, value=entry_config()),
            AppConfig(
                key="retail-catalogue:active", value=catalogue(NOW - timedelta(minutes=91) if stale else NOW)
            ),
        ]
    )
    session.commit()
    transport = FakeTelegramTransport()
    client = TelegramClient(transport, settings)
    message = webhooks.TgMessage.model_validate(
        {
            "message_id": 81,
            "from": {"id": 701},
            "chat": {"id": 701, "type": "private"},
            "text": "/start post_pv0_308",
        }
    )
    webhooks._handle_message(client, message)
    sent = [p for method, p in transport.calls if method == "sendMessage"]
    assert len(sent) == 1
    text = sent[0]["text"]
    if stale:
        assert "100,000" not in text and "50,000" not in text
    else:
        assert "100,000" in text and "20" in text and "31" in text
        assert "50,000" not in text  # below the current minimum wallet top-up
    buttons = [b for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons[0]["url"] == "https://t.me/pvnetwork_bot?start=buy"
    assert {"retail:payment", "retail:status", "retail:setup"} <= {b.get("callback_data") for b in buttons}
    assert "پشتیبانی" in text and "تکمیل" in text
    assert "ورود از لینک این پست" in text
    webhooks._handle_message(client, message)
    assert len([p for method, p in transport.calls if method == "sendMessage"]) == 1
    session.expire_all()
    assert session.query(Event).filter_by(event_type="RETAIL_POST_ENTERED").count() == 1
    assert session.query(Event).filter_by(event_type="PAYMENT_SUCCESS").count() == 0
    assert (
        session.query(FreeAllocation).count()
        == session.query(Job).count()
        == session.query(MessageLog).count()
        == 0
    )


@pytest.mark.parametrize("post", ["pvnetwork0/308", "another/308"])
def test_purchase_status_works_from_this_post_without_requiring_another_buy_start(
    session, settings, monkeypatch, post
):
    from pv_growth.acquisition import purchase_journey
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    user, _ = get_or_create_user(session, telegram_user_id=702)
    ingest(
        session,
        "RETAIL_POST_ENTERED",
        user_id=user.id,
        occurred_at=NOW - timedelta(hours=1),
        idempotency_key="fixture-post",
        metadata={"post": post},
    )
    session.commit()
    monkeypatch.setattr(purchase_journey, "utcnow", lambda: NOW)

    def read(self, uid):
        assert uid == 702
        return {
            "user": {"id": "702", "agent": "f", "User_Status": "Active"},
            "orders": [
                {
                    "Status": "active",
                    "price_product": "123456",
                    "time_sell": str(int((NOW - timedelta(minutes=30)).replace(tzinfo=UTC).timestamp())),
                }
            ],
            "operations": [],
        }

    monkeypatch.setattr(MirzaMySQLReader, "fetch_customer_checkout", read)
    result = purchase_journey.action(session, settings, user, code="status", request_id="fixture-check")
    if post == "pvnetwork0/308":
        assert "123456" in result[0] and "سرویس‌های من" in result[0]
        assert "ابتدا /buy" not in result[0]
    else:
        assert "ابتدا /buy" in result[0]
    assert session.query(Event).filter_by(event_type="PAYMENT_SUCCESS").count() == 0


def test_fixed_summary_counts_only_non_test_entries_inside_its_exact_window():
    from hashlib import sha256

    from pv_growth.acquisition.post_report import summarize_post

    data = {
        "users": [{"id": str(i), "agent": "f"} for i in range(801, 805)],
        "orders": [],
        "operations": [],
        "wallet": [],
    }
    start = datetime(2026, 10, 18, 16, 29, 13, tzinfo=UTC)
    end = start + timedelta(hours=24)
    result = summarize_post(
        data,
        start=start,
        end=end,
        entries={
            "801": [end - timedelta(seconds=1)],
            "802": [end - timedelta(hours=1)],
            "803": [end + timedelta(seconds=1)],
            "804": [start - timedelta(seconds=1)],
        },
        classification={
            "source": "owner_confirmed",
            "all_time_test_account_keys": [sha256(b"mirza-user:802").hexdigest()],
            "ordinary_test_share_estimate_percent": 50,
        },
    )
    assert result["non_test_entered_accounts"] == 1
    assert result["matched"]["service_buyers"] == 0
