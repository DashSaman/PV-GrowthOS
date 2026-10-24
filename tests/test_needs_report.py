import importlib.util
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from pv_growth.database.models import AppConfig, Job

START = datetime(2026, 10, 24, 11, tzinfo=UTC)
NOW = START + timedelta(hours=2)
CODES = [f"social:needs261024_n{i:02}" for i in range(1, 21)]


def module():
    assert importlib.util.find_spec("pv_growth.acquisition.needs_report"), "ten-route native report missing"
    from pv_growth.acquisition import needs_report

    return needs_report


def quality():
    return {
        "source": "owner_confirmed",
        "all_time_test_account_keys": [sha256(f"mirza-user:{i}".encode()).hexdigest() for i in (2, 3, 4)],
        "ordinary_test_share_estimate_percent": 50,
    }


def registry():
    return {
        "stage": "registered",
        "campaign": "needs-led-acquisition-20261024",
        "registered_at_utc": START.isoformat(),
        "owner_authorized_limit": 20,
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
    assert len(r["sources"]) == 20
    assert r["non_test_entered_accounts"] == 1
    assert r["sources"][CODES[0]]["service_buyers"] == 1
    assert r["sources"][CODES[1]]["service_buyers"] == 0
    assert r["matched"] == {"service_buyers": 1, "service_records": 1, "recorded_service_toman": "100000"}
    assert r["confirmed_internal_test_account_count"] == 3
    assert r["owner_50_percent_estimate_applied"] is False
    assert r["campaign"] == "needs-led-acquisition-20261024"


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
        value["owner_authorized_limit"] = 21
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
    assert len(jobs) == 1 and jobs[0].job_type == "retail.needs_report"
    assert jobs[0].max_attempts == 1
    assert jobs[0].idempotency_key.startswith("retail-needs-report:20261024:")


def test_incomplete_internal_test_classification_is_unavailable_not_zero():
    p = module()
    q = quality()
    q["all_time_test_account_keys"].pop()
    with pytest.raises(ValueError):
        p.summarize_outreach(
            snapshot(), start=START, end=NOW, entries=[], source_codes=CODES, classification=q
        )


def test_existing_customer_history_is_separate_from_unverified_newness():
    p = module()
    data = snapshot()
    data["orders"].append(
        dict(
            data["orders"][0], id_invoice="older", time_sell=str(int((START - timedelta(days=2)).timestamp()))
        )
    )
    report = p.summarize_outreach(
        data,
        start=START,
        end=NOW,
        entries=[("1", CODES[0], START)],
        source_codes=CODES,
        classification=quality(),
    )
    assert report["matched_existing_customer_buyers"] == 1
    assert report["matched_without_earlier_positive_history_buyers"] == 0
    assert report["verified_new_customer_count"] is None


def test_first_positive_purchase_is_not_automatically_a_verified_new_customer():
    p = module()
    report = p.summarize_outreach(
        snapshot(),
        start=START,
        end=NOW,
        entries=[("1", CODES[0], START)],
        source_codes=CODES,
        classification=quality(),
    )
    assert report["matched_existing_customer_buyers"] == 0
    assert report["matched_without_earlier_positive_history_buyers"] == 1
    assert report["verified_new_customer_count"] is None


def test_review72_uses_actual_acceptance_time_and_fixed_deadline():
    p = module()
    reg = registry()
    assert p.review_deadline(reg) is None
    reg["initial_send_closed"] = True
    reg["accepted_initial_count"] = 20
    reg["last_accepted_at_utc"] = (START + timedelta(minutes=17)).isoformat()
    deadline = p.review_deadline(reg)
    assert deadline == START + timedelta(hours=72, minutes=17)
    report = {
        "status": "ok",
        "data_complete": True,
        "start_utc": START.isoformat(),
        "end_utc": (deadline + timedelta(hours=1)).isoformat(),
    }
    assert (
        p.review72(report, reg, deadline - timedelta(seconds=1), lambda end: pytest.fail("early report"))
        is None
    )
    calls = []
    result = p.review72(
        report,
        reg,
        deadline + timedelta(hours=1),
        lambda end: calls.append(end) or dict(report, end_utc=end.isoformat()),
    )
    assert calls == [deadline] and result["end_utc"] == deadline.isoformat()
    assert result["review_hours"] == 72 and result["review_deadline_utc"] == deadline.isoformat()


def test_review72_does_not_cache_incomplete_source_as_zero():
    p = module()
    reg = registry()
    reg.update(initial_send_closed=True, accepted_initial_count=20, last_accepted_at_utc=START.isoformat())
    assert (
        p.review72(
            {"status": "unavailable", "data_complete": False},
            reg,
            START + timedelta(hours=73),
            lambda end: pytest.fail("incomplete"),
        )
        is None
    )


def test_native_monitor_saves_fixed_review_once_after_actual_deadline(session, settings, monkeypatch):
    from types import SimpleNamespace

    from pv_growth.database.models import Source

    p = module()
    reg = registry()
    reg.update(initial_send_closed=True, accepted_initial_count=20, last_accepted_at_utc=START.isoformat())
    session.add(AppConfig(key=p.CONFIG_KEY, value=reg))
    session.add(AppConfig(key="retail-sales:classification", value=quality()))
    for code in CODES:
        session.add(Source(code=code, kind="social", medium="business_email"))
    session.commit()
    now = START + timedelta(hours=73)
    monkeypatch.setattr(p, "utcnow", lambda: now)
    monkeypatch.setattr(
        p, "MirzaMySQLReader", lambda _: SimpleNamespace(fetch_retail_sales_snapshot=snapshot)
    )
    p.monitor(session, settings, {"hour": now.strftime("%Y%m%d%H")})
    result = session.get(AppConfig, p.PREFIX + ":milestone:72").value
    assert result["end_utc"] == (START + timedelta(hours=72)).isoformat()
    assert result["verified_new_customer_count"] is None
    now += timedelta(hours=1)
    p.monitor(session, settings, {"hour": now.strftime("%Y%m%d%H")})
    assert session.get(AppConfig, p.PREFIX + ":milestone:72").value == result
