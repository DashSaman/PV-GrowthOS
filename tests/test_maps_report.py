import importlib.util
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from pv_growth.database.models import AppConfig, Job

START = datetime(2026, 10, 20, 11, tzinfo=UTC)
NOW = START + timedelta(hours=2)
CODES = [f"social:maps261020_m{i:02}" for i in range(1, 11)]


def module():
    assert importlib.util.find_spec("pv_growth.acquisition.maps_report"), "ten-route native report missing"
    from pv_growth.acquisition import maps_report

    return maps_report


def quality():
    return {
        "source": "owner_confirmed",
        "all_time_test_account_keys": [sha256(f"mirza-user:{i}".encode()).hexdigest() for i in (2, 3, 4)],
        "ordinary_test_share_estimate_percent": 50,
    }


def registry():
    return {
        "stage": "registered",
        "campaign": "maps-acquisition-20261020",
        "registered_at_utc": START.isoformat(),
        "owner_authorized_limit": 10,
        "sources": [
            {
                "key": c.rsplit("_", 1)[1],
                "start_parameter": c.replace("social:", "social_"),
                "source_code": c,
                "segment": "business",
            }
            for c in CODES
        ],
    }


def snapshot():
    return {
        "users": [{"id": str(i), "agent": "f"} for i in range(1, 5)],
        "orders": [
            {
                "id_invoice": str(i),
                "id_user": str(i),
                "Status": "active",
                "price_product": "100000",
                "time_sell": str(int((START + timedelta(hours=1)).timestamp())),
            }
            for i in range(1, 5)
        ],
        "operations": [],
        "wallet": [
            {
                "id_user": "1",
                "payment_Status": "paid",
                "price": "900000",
                "time": str(int((START + timedelta(hours=1)).timestamp())),
            }
        ],
    }


def test_ten_routes_exclude_every_order_of_three_test_accounts_without_halving_real_sale():
    p = module()
    entries = [(str(i), CODES[0], START) for i in range(1, 5)]
    entries += [("1", CODES[1], START + timedelta(minutes=20))]
    r = p.summarize_outreach(
        snapshot(), start=START, end=NOW, entries=entries, source_codes=CODES, classification=quality()
    )
    assert len(r["sources"]) == 10
    assert r["non_test_entered_accounts"] == 1
    assert r["sources"][CODES[0]]["service_buyers"] == 1
    assert r["sources"][CODES[1]]["service_buyers"] == 0
    assert r["matched"] == {"service_buyers": 1, "service_records": 1, "recorded_service_toman": "100000"}
    assert r["confirmed_internal_test_account_count"] == 3
    assert r["owner_50_percent_estimate_applied"] is False
    assert r["campaign"] == "maps-acquisition-20261020"


@pytest.mark.parametrize("change", ["count", "old_route", "duplicate", "limit"])
def test_registry_rejects_widening_or_mixing_the_original_thirty(change):
    p = module()
    value = registry()
    if change == "count":
        value["sources"].pop()
    elif change == "old_route":
        value["sources"][0]["source_code"] = "social:ext261019_a01"
    elif change == "duplicate":
        value["sources"][-1] = value["sources"][0]
    else:
        value["owner_authorized_limit"] = 11
    with pytest.raises(ValueError):
        p.configuration(value, NOW)


def test_native_job_needs_registry_and_is_one_attempt_per_hour(session):
    p = module()
    now = NOW.replace(tzinfo=None)
    p.enqueue_monitor(session, now)
    assert session.query(Job).count() == 0
    session.add(AppConfig(key=p.CONFIG_KEY, value=registry()))
    session.commit()
    p.enqueue_monitor(session, now)
    p.enqueue_monitor(session, now)
    session.commit()
    jobs = session.query(Job).all()
    assert len(jobs) == 1 and jobs[0].job_type == "retail.maps_report"
    assert jobs[0].max_attempts == 1
    assert jobs[0].idempotency_key.startswith("retail-maps-report:20261020:")


def test_incomplete_internal_test_classification_is_unavailable_not_zero():
    p = module()
    q = quality()
    q["all_time_test_account_keys"].pop()
    with pytest.raises(ValueError):
        p.summarize_outreach(
            snapshot(), start=START, end=NOW, entries=[], source_codes=CODES, classification=q
        )
