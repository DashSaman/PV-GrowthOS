from datetime import UTC, datetime

from pv_growth.acquisition import expansion_mail_jobs, mail_jobs, maps_mail_jobs, needs_mail_jobs


def at(minute, second=0):
    return datetime(2026, 10, 25, 12, minute, second, tzinfo=UTC)


def test_pollers_have_separate_native_start_minutes_without_new_idempotency_namespaces():
    assert mail_jobs.bucket(at(0)) == "202610251200"
    assert maps_mail_jobs.bucket(at(4, 59)) == "202610251145"
    assert maps_mail_jobs.bucket(at(5)) == "202610251200"
    assert needs_mail_jobs.bucket(at(9, 59)) == "202610251145"
    assert needs_mail_jobs.bucket(at(10)) == "202610251200"
    assert expansion_mail_jobs.bucket(at(1, 59)) == "202610251155"
    assert expansion_mail_jobs.bucket(at(2)) == "202610251200"
