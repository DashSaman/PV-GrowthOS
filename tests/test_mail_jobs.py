import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from pv_growth.acquisition import mail_jobs
from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig, Job

NOW = datetime(2026, 10, 19, 18, 5, tzinfo=UTC)


def configuration():
    return {
        "campaign": "external-acquisition-20261019",
        "owner": "pvnetwork8@gmail.com",
        "authorized_until_utc": (NOW + timedelta(days=7)).isoformat(),
        "credentials": {"client_id": "fixture", "client_secret": "fixture", "refresh_token": "fixture"},
        "recipients": [
            {
                "route": f"{kind}{i:02}",
                "email": f"{kind}{i}@example.org",
                "subject": "Original proposal",
                "name": "Business",
            }
            for kind, cap in [("a", 10), ("s", 20)]
            for i in range(1, cap + 1)
        ],
    }


def test_no_config_means_no_scheduler_mail_job(session, tmp_path, monkeypatch):
    monkeypatch.setattr(mail_jobs, "CONFIG_PATH", tmp_path / "absent.json")
    mail_jobs.enqueue(session, NOW)
    assert session.query(Job).filter_by(job_type="retail.mail_replies").count() == 0


def test_scheduler_enqueues_real_mail_job_when_private_file_exists(session, settings, tmp_path, monkeypatch):
    from pv_growth.jobs.scheduler import Scheduler

    path = tmp_path / "private.json"
    path.write_text(json.dumps(configuration()), encoding="utf-8")
    monkeypatch.setattr(mail_jobs, "CONFIG_PATH", path)
    monkeypatch.setattr("pv_growth.database.types.utcnow", lambda: NOW)
    Scheduler(settings)._enqueue_periodic()
    session.expire_all()
    rows = session.query(Job).filter_by(job_type="retail.mail_replies").all()
    assert len(rows) == 1 and rows[0].max_attempts == 1


def test_native_jobs_are_once_per_quarter_hour_with_one_attempt(session, tmp_path, monkeypatch):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(configuration()), encoding="utf-8")
    monkeypatch.setattr(mail_jobs, "CONFIG_PATH", path)
    mail_jobs.enqueue(session, NOW)
    mail_jobs.enqueue(session, NOW + timedelta(minutes=2))
    mail_jobs.enqueue(session, NOW + timedelta(minutes=15))
    jobs = session.query(Job).filter_by(job_type="retail.mail_replies").all()
    assert len(jobs) == 2 and all(j.max_attempts == 1 for j in jobs)
    assert jobs[0].payload == {"bucket": "202610191800"}
    assert "fixture" not in str([j.payload for j in jobs])


def test_stale_job_never_opens_auth_or_sends(session, settings, monkeypatch):
    monkeypatch.setattr(mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(mail_jobs, "load_config", lambda *_: pytest.fail("must not read credentials"))
    mail_jobs.monitor(session, settings, {"bucket": "202610191745"})
    assert session.get(AppConfig, "mail-replies:20261019:latest") is None


def test_missing_auth_is_unavailable_and_saved_once(session, settings, monkeypatch):
    monkeypatch.setattr(mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(mail_jobs, "load_config", lambda *_: (_ for _ in ()).throw(ValueError("private")))
    mail_jobs.monitor(session, settings, {"bucket": "202610191800"})
    report = session.get(AppConfig, "mail-replies:20261019:latest").value
    assert report["status"] == "unavailable" and report["data_complete"] is False
    assert "private" not in str(report)
    monkeypatch.setattr(mail_jobs, "load_config", lambda *_: pytest.fail("do not replay saved bucket"))
    mail_jobs.monitor(session, settings, {"bucket": "202610191800"})


def test_loader_rejects_oversized_json_and_symlink(settings, tmp_path, monkeypatch):
    path = tmp_path / "private.json"
    path.write_bytes(b" " * 65537)
    monkeypatch.setattr(mail_jobs, "CONFIG_PATH", path)
    with pytest.raises(ValueError):
        mail_jobs.load_config(settings, NOW)


def test_absent_test_classification_stops_before_gmail(session, settings, monkeypatch):
    monkeypatch.setattr(mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(mail_jobs, "load_config", lambda *_: configuration())
    monkeypatch.setattr(mail_jobs, "GmailTransport", lambda **_: pytest.fail("Gmail must remain unused"))
    mail_jobs.monitor(session, settings, {"bucket": "202610191800"})
    assert session.get(AppConfig, "mail-replies:20261019:latest").value["status"] == "unavailable"


def test_config_loader_rejects_expiry_without_returning_credentials(settings, tmp_path, monkeypatch):
    value = configuration()
    value["authorized_until_utc"] = NOW.isoformat()
    path = tmp_path / "private.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(mail_jobs, "CONFIG_PATH", path)
    with pytest.raises(ValueError) as error:
        mail_jobs.load_config(settings, NOW)
    assert "fixture" not in str(error.value)


def test_full_native_cycle_requires_existing_registry_and_test_exclusions(session, settings, monkeypatch):
    from pv_growth.acquisition.outreach_report import SOURCE_CODES

    registry = {
        "campaign": "external-acquisition-20261019",
        "stage": "registered",
        "owner_authorized_limit": 30,
        "registered_at_utc": (NOW - timedelta(hours=1)).isoformat(),
        "sources": [
            {
                "source_code": code,
                "key": code.rsplit("_", 1)[1],
                "start_parameter": code.replace("social:", "social_", 1),
                "segment": "admin" if "_a" in code else "shop",
            }
            for code in SOURCE_CODES
        ],
    }
    session.add(AppConfig(key="retail-outreach:campaign:20261019", value=registry))
    session.add(
        AppConfig(
            key="retail-sales:classification",
            value={
                "source": "owner_confirmed",
                "all_time_test_account_keys": [sha256(f"test-{i}".encode()).hexdigest() for i in range(3)],
            },
        )
    )
    session.add(
        AppConfig(
            key="retail-catalogue:active",
            value={
                "source": "mirza_local_readonly",
                "observed_at_utc": NOW.isoformat(),
                "plans": [
                    {
                        "code": "fixture",
                        "name": "fixture",
                        "location": "fixture",
                        "volume_gb": 20,
                        "duration_days": 31,
                        "price_toman": 100000,
                    }
                ],
                "payment": {"direct_buy": False, "minimum_wallet_toman": 100000, "card_to_support": True},
            },
        )
    )
    session.commit()
    monkeypatch.setattr(mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(mail_jobs, "load_config", lambda *_: configuration())

    class EmptyGmail:
        def __init__(self, **kwargs):
            self.searches = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def find_sent(self, route):
            self.searches.append(route)
            return {"threads": []}

    monkeypatch.setattr(mail_jobs, "GmailTransport", EmptyGmail)
    mail_jobs.monitor(session, settings, {"bucket": "202610191800"})
    with session_scope(settings) as check:
        report = check.get(AppConfig, "mail-replies:20261019:latest").value
    assert report["status"] == "ok" and len(report["routes"]) == 30
    assert report["confirmed_internal_test_account_count"] == 3
    assert report["reminders_sent"] == 0 and report["execution_owner"] == "docker_scheduler"
