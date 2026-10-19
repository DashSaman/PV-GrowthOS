import importlib.util
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from pv_growth.database.models import AppConfig, Event, Job, MessageLog, Source
from pv_growth.events.service import get_or_create_user

NOW = datetime(2026, 10, 19, 12, 5)
START = datetime(2026, 10, 19, 10, tzinfo=UTC)
CODES = [f"social:ext261019_a{i:02}" for i in range(1, 11)] + [
    f"social:ext261019_s{i:02}" for i in range(1, 21)
]


def module():
    assert importlib.util.find_spec("pv_growth.acquisition.outreach_report"), (
        "native outreach report is missing"
    )
    from pv_growth.acquisition import outreach_report

    return outreach_report


def registry():
    return {
        "stage": "registered",
        "campaign": "external-acquisition-20261019",
        "registered_at_utc": START.isoformat(),
        "owner_authorized_limit": 30,
        "sources": [
            {
                "key": c.rsplit("_", 1)[1],
                "start_parameter": c.replace("social:", "social_", 1),
                "source_code": c,
                "segment": "admin" if c.rsplit("_", 1)[1].startswith("a") else "shop",
            }
            for c in CODES
        ],
    }


def classification():
    return {
        "source": "owner_confirmed",
        "all_time_test_account_keys": [sha256(b"mirza-user:102").hexdigest()],
        "ordinary_test_share_estimate_percent": 50,
    }


def stamp(hour):
    return str(int(datetime(2026, 10, 19, hour, tzinfo=UTC).timestamp()))


def snapshot():
    return {
        "users": [{"id": str(i), "agent": "n2" if i == 103 else "f"} for i in range(101, 106)],
        "orders": [
            {
                "id_invoice": f"invoice-{i}",
                "id_user": str(i),
                "Status": status,
                "price_product": price,
                "time_sell": stamp(hour),
            }
            for i, status, price, hour in [
                (101, "active", "100000", 11),
                (102, "active", "900000", 11),
                (103, "active", "400000", 11),
                (104, "active", "80000", 9),
                (105, "unpaid", "600000", 11),
            ]
        ],
        "operations": [
            {"id_user": "101", "type": "extend_user", "status": "paid", "price": "30000", "time": stamp(11)}
            for _ in range(2)
        ],
        "wallet": [{"id_user": "101", "payment_Status": "paid", "price": "50000", "time": stamp(11)}],
    }


def entries():
    return [
        ("101", CODES[0], START),
        ("101", CODES[0], START),
        ("101", CODES[1], START + timedelta(minutes=30)),
        ("102", CODES[0], START),
        ("103", CODES[1], START),
        ("104", CODES[2], START),
        ("105", CODES[3], START),
    ]


def seed(session):
    p = module()
    session.add(AppConfig(key=p.CONFIG_KEY, value=registry()))
    session.add(AppConfig(key="retail-sales:classification", value=classification()))
    session.add_all([Source(code=c, kind="social", medium="business_email") for c in CODES])
    for uid, code, when in entries():
        user, _ = get_or_create_user(session, telegram_user_id=int(uid))
        source = session.query(Source).filter_by(code=code).one()
        session.add(
            Event(
                event_id=str(uuid4()),
                idempotency_key=str(uuid4()),
                event_type="SOURCE_ATTRIBUTED",
                user_id=user.id,
                source_id=source.id,
                occurred_at=when.replace(tzinfo=None),
                metadata_json={},
            )
        )
    session.commit()


def test_per_source_visits_and_first_campaign_purchase_credit_exclude_tests_and_wallet():
    p = module()
    report = p.summarize_outreach(
        snapshot(),
        start=START,
        end=NOW.replace(tzinfo=UTC),
        entries=entries(),
        source_codes=CODES,
        classification=classification(),
    )
    assert len(report["sources"]) == 30
    assert report["sources"][CODES[0]]["entered_accounts"] == 1
    assert report["sources"][CODES[1]]["entered_accounts"] == 2
    assert report["sources"][CODES[1]]["service_records"] == 0
    assert report["matched"] == {
        "service_buyers": 1,
        "service_records": 3,
        "recorded_service_toman": "160000",
    }
    assert report["non_test_entered_accounts"] == 4
    assert report["confirmed_internal_test_account_count"] == 1
    assert report["owner_50_percent_estimate_applied"] is False
    assert report["external_customer_verified"] is False
    assert "invoice-101" not in str(report) and "mirza-user:" not in str(report)


def test_fixed_boundaries_future_entries_and_seven_day_limit():
    p = module()
    old = START - timedelta(days=8)
    report = p.summarize_outreach(
        snapshot(),
        start=old,
        end=NOW.replace(tzinfo=UTC),
        entries=[("101", CODES[0], old), ("102", CODES[0], NOW.replace(tzinfo=UTC) + timedelta(hours=1))],
        source_codes=CODES,
        classification=classification(),
    )
    assert report["matched"]["service_buyers"] == 0
    assert report["sources"][CODES[0]]["entered_accounts"] == 1


def test_unknown_test_classification_and_bad_business_dates_fail_closed():
    p = module()
    for value in (None, {"source": "guessed", "all_time_test_account_keys": []}):
        with pytest.raises(ValueError):
            p.summarize_outreach(
                snapshot(),
                start=START,
                end=NOW.replace(tzinfo=UTC),
                entries=entries(),
                source_codes=CODES,
                classification=value,
            )
    broken = snapshot()
    broken["orders"][0]["time_sell"] = "bad"
    with pytest.raises(ValueError):
        p.summarize_outreach(
            broken,
            start=START,
            end=NOW.replace(tzinfo=UTC),
            entries=entries(),
            source_codes=CODES,
            classification=classification(),
        )


def test_hourly_job_is_idempotent_and_stale_or_duplicate_payloads_never_reread(
    session, settings, monkeypatch
):
    p = module()
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    seed(session)
    monkeypatch.setattr(p, "utcnow", lambda: NOW)
    calls = []

    def read(self):
        calls.append(1)
        return snapshot()

    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", read)
    p.enqueue_monitor(session, NOW)
    p.enqueue_monitor(session, NOW + timedelta(minutes=2))
    session.flush()
    jobs = session.query(Job).filter_by(job_type="retail.outreach_report").all()
    assert len(jobs) == 1 and jobs[0].max_attempts == 1
    p.monitor(session, settings, {"hour": "2026101911"})
    assert not calls
    p.monitor(session, settings, jobs[0].payload)
    p.monitor(session, settings, jobs[0].payload)
    assert calls == [1]
    report = session.get(AppConfig, p.PREFIX + ":latest").value
    assert report["status"] == "ok" and report["data_complete"] is True
    assert report["execution_owner"] == "docker_scheduler" and report["customer_messages"] == 0
    assert report["matched"]["recorded_service_toman"] == "160000"
    assert session.get(AppConfig, p.PREFIX + ":hour:2026101912") is not None
    assert session.query(MessageLog).count() == 0
    assert session.query(Event).count() == len(entries())


def test_source_or_classification_failure_is_unavailable_not_zero(session, settings, monkeypatch):
    p = module()
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    seed(session)
    monkeypatch.setattr(p, "utcnow", lambda: NOW)

    def fail(self):
        raise RuntimeError("no credentials or raw source details may be exported")

    monkeypatch.setattr(MirzaMySQLReader, "fetch_retail_sales_snapshot", fail)
    p.monitor(session, settings, {"hour": "2026101912"})
    report = session.get(AppConfig, p.PREFIX + ":latest").value
    assert report["status"] == "unavailable" and report["data_complete"] is False
    assert "matched" not in report and "credentials" not in str(report)
    assert session.query(MessageLog).count() == 0


def test_exact_registry_and_source_rows_are_required(session, settings, monkeypatch):
    p = module()
    seed(session)
    monkeypatch.setattr(p, "utcnow", lambda: NOW)
    row = session.get(AppConfig, p.CONFIG_KEY)
    value = registry()
    value["sources"][0]["source_code"] = "social:another_campaign"
    row.value = value
    session.commit()
    p.monitor(session, settings, {"hour": "2026101912"})
    assert session.get(AppConfig, p.PREFIX + ":latest").value["status"] == "unavailable"


def test_scheduler_and_runner_register_native_report(session, settings, monkeypatch):
    p = module()
    from pv_growth.jobs.runner import _HANDLERS, register_builtin_handlers
    from pv_growth.jobs.scheduler import Scheduler

    seed(session)
    monkeypatch.setattr(p, "utcnow", lambda: NOW)
    monkeypatch.setattr("pv_growth.database.types.utcnow", lambda: NOW)
    Scheduler(settings)._enqueue_periodic()
    session.expire_all()
    assert session.query(Job).filter_by(job_type="retail.outreach_report").count() == 1
    register_builtin_handlers()
    assert _HANDLERS["retail.outreach_report"] is p.monitor


def test_cached_private_text_has_all_paths_without_reading_mirza(session, monkeypatch):
    p = module()
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    assert p.report_text(session) == ""
    report = p.summarize_outreach(
        snapshot(),
        start=START,
        end=NOW.replace(tzinfo=UTC),
        entries=entries(),
        source_codes=CODES,
        classification=classification(),
    )
    report["observed_at_utc"] = NOW.replace(tzinfo=UTC).isoformat()
    session.add(AppConfig(key=p.PREFIX + ":latest", value=report))
    session.commit()
    monkeypatch.setattr(
        MirzaMySQLReader, "fetch_retail_sales_snapshot", lambda self: pytest.fail("cache only")
    )
    body = p.report_text(session)
    assert all(c.rsplit("_", 1)[1] in body for c in CODES)
    assert "160000" in body and len(body) < 4096


def test_final_fixed_window_stops_new_jobs_only_after_complete_report(session):
    p = module()
    seed(session)
    final = START + timedelta(days=31)
    session.add(
        AppConfig(
            key=p.PREFIX + ":latest",
            value={"status": "ok", "data_complete": True, "end_utc": final.isoformat()},
        )
    )
    session.commit()
    p.enqueue_monitor(session, (final + timedelta(hours=1)).replace(tzinfo=None))
    assert session.query(Job).filter_by(job_type="retail.outreach_report").count() == 0
