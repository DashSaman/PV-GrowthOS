from datetime import UTC, datetime, timedelta

from pv_growth.database.models import AppConfig

NOW = datetime(2026, 10, 18, 15, tzinfo=UTC)


def catalogue():
    return {
        "source": "mirza_local_readonly",
        "observed_at_utc": NOW.isoformat(),
        "plans": [
            {
                "code": "a",
                "name": "تانل ۱۰ گیگ",
                "volume_gb": 10,
                "duration_days": 31,
                "price_toman": 50000,
                "location": "تانل",
            },
            {
                "code": "b",
                "name": "تانل ۲۰ گیگ",
                "volume_gb": 20,
                "duration_days": 31,
                "price_toman": 100000,
                "location": "تانل",
            },
        ],
        "payment": {
            "direct_buy": False,
            "minimum_wallet_toman": 100000,
            "card_to_support": True,
            "support_handle": "PVNetwork1",
        },
    }


def test_fresh_source_prices_and_wallet_minimum_drive_offer(session):
    from pv_growth.acquisition.retail_catalogue import catalogue_text, current_catalogue

    session.add(AppConfig(key="retail-catalogue:active", value=catalogue()))
    session.commit()
    live = current_catalogue(session, now=NOW + timedelta(minutes=1))
    assert live["recommended"]["code"] == "b"
    text = catalogue_text(live)
    assert "100,000" in text and "50,000" in text
    assert "کیف پول" in text and "تأیید" in text
    assert "b" not in text  # internal product codes are not customer copy


def test_expired_untrusted_or_partial_catalogues_are_hidden(session):
    from pv_growth.acquisition.retail_catalogue import current_catalogue

    row = AppConfig(key="retail-catalogue:active", value=catalogue())
    session.add(row)
    session.commit()
    assert current_catalogue(session, now=NOW + timedelta(minutes=91)) is None
    assert current_catalogue(session, now=NOW - timedelta(seconds=1)) is None
    value = catalogue()
    value["plans"][0]["price_toman"] = "not-a-price"
    row.value = value
    session.commit()
    assert current_catalogue(session, now=NOW) is None


def test_catalogue_is_used_in_real_requested_guide_without_creating_an_order(session):
    from pv_growth.acquisition.retail_offer import record_request
    from pv_growth.events.service import get_or_create_user

    user, _ = get_or_create_user(session, telegram_user_id=123)
    session.add(AppConfig(key="retail-catalogue:active", value=catalogue()))
    session.commit()
    from unittest.mock import patch

    with patch("pv_growth.acquisition.retail_catalogue.utcnow", return_value=NOW):
        result = record_request(session, user, source="web", request_id="live-price")
    assert "100,000" in result[0]
    assert "قیمت تأییدشده‌ای برای نمایش" not in result[0]
    assert result[1].buttons[0][0]["url"] == "https://t.me/pvnetwork_bot?start=buy"


def test_payment_guide_matches_wallet_then_manual_support_and_purchase_completion(session):
    from pv_growth.acquisition.retail_catalogue import payment_text

    text = payment_text(catalogue())
    assert "100,000" in text
    assert "@PVNetwork1" in text
    assert "خرید سرویس را تکمیل" in text
    assert "دوباره پرداخت نکن" in text
    assert "پرداخت فوری" not in text


def test_import_validates_before_overwriting_only_public_catalogue(session):
    from unittest.mock import patch

    import pytest

    from pv_growth.acquisition.catalogue_import import store

    with patch("pv_growth.acquisition.retail_catalogue.utcnow", return_value=NOW):
        assert store(session, catalogue()) == 2
        before = session.get(AppConfig, "retail-catalogue:active").value
        with pytest.raises(ValueError):
            store(session, {"source": "invented", "plans": []})
        assert session.get(AppConfig, "retail-catalogue:active").value == before
    from pv_growth.database.models import Event, FreeAllocation, Job, MessageLog

    assert session.query(Job).count() == 0
    assert session.query(Event).count() == 0
    assert session.query(FreeAllocation).count() == 0
    assert session.query(MessageLog).count() == 0
