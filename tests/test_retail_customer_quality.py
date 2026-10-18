from datetime import UTC, datetime

from pv_growth.mirza_adapter.retail_sales import render_report, summarize
from tests.test_retail_sales import snapshot

START = datetime(2026, 10, 15, tzinfo=UTC)
END = datetime(2026, 10, 16, tzinfo=UTC)


def test_unclassified_ordinary_records_are_not_verified_external_retail_sales():
    report = summarize(snapshot(), start=START, end=END)
    assert report["customer_quality"]["external_retail_verified"] is False
    assert report["customer_quality"]["classification_status"] == "unavailable"
    assert "تست داخلی" in render_report(report)
    assert "تفکیک‌نشده" in render_report(report)


def test_owner_estimate_is_a_scenario_not_a_transaction_or_buyer_exclusion():
    report = summarize(
        snapshot(),
        start=START,
        end=END,
        classification={
            "source": "owner_confirmed",
            "all_time_test_account_keys": [],
            "ordinary_test_share_estimate_percent": 50,
        },
    )
    quality = report["current"]["customer_quality"]
    assert quality["ordinary_remaining_unverified"]["recorded_service_toman"] == "150000"
    assert quality["ordinary_remaining_unverified"]["service_buyers"] == 1
    assert quality["owner_estimate_scenario"]["estimated_non_test_service_toman"] == "75000"
    assert "service_buyers" not in quality["owner_estimate_scenario"]


def test_confirmed_test_accounts_are_separate_without_removing_raw_history():
    from hashlib import sha256

    account_key = sha256(b"mirza-user:82").hexdigest()
    report = summarize(
        snapshot(),
        start=START,
        end=END,
        classification={
            "source": "owner_confirmed",
            "all_time_test_account_keys": [account_key],
            "ordinary_test_share_estimate_percent": 50,
        },
    )
    assert report["current"]["segments"]["cooperation"]["recorded_service_toman"] == "800000"
    quality = report["current"]["customer_quality"]
    assert quality["confirmed_internal_test"]["recorded_service_toman"] == "800000"
    assert quality["confirmed_internal_test"]["service_buyers"] == 1
    assert quality["remaining_segments"]["cooperation"]["recorded_service_toman"] == "0"
    assert quality["ordinary_remaining_unverified"]["recorded_service_toman"] == "150000"
    assert account_key not in str(report)


def test_malformed_classification_cannot_hide_transactions():
    report = summarize(
        snapshot(),
        start=START,
        end=END,
        classification={
            "source": "owner_confirmed",
            "all_time_test_account_keys": ["82"],
            "ordinary_test_share_estimate_percent": True,
        },
    )
    assert report["customer_quality"]["classification_status"] == "invalid"
    assert report["current"]["customer_quality"]["confirmed_internal_test"] is None
    assert report["current"]["segments"]["cooperation"]["recorded_service_toman"] == "800000"


def test_native_report_loads_private_owner_classification(session, settings, monkeypatch):
    from pv_growth.database.models import AppConfig
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
    from pv_growth.mirza_adapter.retail_sales import collect_report

    session.add(
        AppConfig(
            key="retail-sales:classification",
            value={
                "source": "owner_confirmed",
                "all_time_test_account_keys": [],
                "ordinary_test_share_estimate_percent": 50,
            },
        )
    )
    session.commit()
    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", lambda self: snapshot())
    report = collect_report(session, settings, key="retail-sales:quality-test", end=END)
    assert (
        report["current"]["customer_quality"]["owner_estimate_scenario"]["estimated_non_test_service_toman"]
        == "75000"
    )
    assert report["customer_quality"]["external_retail_verified"] is False
    # Immutable historical caches are rendered with the same caveat, never rewritten.
    old = dict(report)
    old.pop("customer_quality")
    old["current"] = dict(report["current"])
    old["current"].pop("customer_quality")
    assert "تست داخلی" in render_report(old)
