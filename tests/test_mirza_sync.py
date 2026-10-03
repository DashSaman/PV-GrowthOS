"""Mirza read-only MySQL sync: seen-set scan, idempotent events, transitions,
outage isolation."""

import uuid

import pytest

from pv_growth.database.models import AppConfig, Event, User
from pv_growth.mirza_adapter.mysql_reader import (
    WATERMARK_KEY,
    FakeMirzaMySQL,
    sync_mirza,
)


def _invoice(inv_id, tg, status="active", price="990,000", refral=""):
    return {"id_invoice": inv_id, "id_user": str(tg), "username": f"u{tg}",
            "name_product": "1-month", "price_product": price,
            "Volume": "30", "Service_time": "30", "Status": status,
            "refral": refral}


@pytest.fixture(autouse=True)
def _reset_state(session):
    """The test DB is shared across the session; sync tests start clean."""
    session.query(AppConfig).filter_by(key=WATERMARK_KEY).delete()
    session.commit()


def test_first_scan_ingests_history_once(session, settings):
    """First run ingests the existing stock once (paid + unpaid), then nothing."""
    tg = int(f"98{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL([
        _invoice("aa01", tg, "active"),
        _invoice("aa02", tg, "unpaid"),
        _invoice("aa03", tg, "weirdstatus"),  # unmapped status -> skipped, still seen
    ])
    s1 = sync_mirza(session, settings, reader)
    assert s1["new"] == 3 and s1["payments"] == 1 and s1["checkouts"] == 1
    assert s1["skipped"] == 1

    s2 = sync_mirza(session, settings, reader)  # all seen now
    assert s2["new"] == 0 and s2["payments"] == 0
    assert session.query(Event).filter(
        Event.idempotency_key.like("mirza:%")).count() == 3
    state = session.get(AppConfig, WATERMARK_KEY)
    assert state.value["count"] == 3
    assert session.query(User).filter_by(telegram_user_id=tg).count() == 1


def test_new_invoice_and_unpaid_to_paid_transition(session, settings):
    tg = int(f"97{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL([_invoice("bb01", tg, "unpaid")])
    sync_mirza(session, settings, reader)
    assert session.query(Event).filter_by(
        idempotency_key="mirza:checkout:bb01").count() == 1

    # the same invoice settles: status flips to active -> PAYMENT_SUCCESS fires
    reader.invoices = [_invoice("bb01", tg, "active"), _invoice("bb02", tg, "active")]
    s = sync_mirza(session, settings, reader)
    assert s["payments"] == 1  # only bb02 is new; bb01 already seen
    # replay everything twice more: still no duplicates
    sync_mirza(session, settings, reader)
    keys = [k for (k,) in session.query(Event.idempotency_key)
            .filter(Event.idempotency_key.like("mirza:%:bb%")).all()]
    assert sorted(keys) == ["mirza:checkout:bb01", "mirza:pay:bb02", "mirza:svc:bb02"]


def test_scan_respects_limit(session, settings):
    invoices = [_invoice(f"cc{i:02d}", 500000000 + i, "active") for i in range(5)]
    reader = FakeMirzaMySQL(invoices)
    s1 = sync_mirza(session, settings, reader, limit=3)
    assert s1["new"] == 3
    s2 = sync_mirza(session, settings, reader, limit=3)
    assert s2["new"] == 2  # picks up the rest on the next cycle


def test_sync_failure_isolated(session, settings):
    class BrokenReader:
        def list_invoice_ids(self):
            raise RuntimeError("mysql down")

        def fetch_by_ids(self, ids):
            raise RuntimeError("mysql down")

    stats = sync_mirza(session, settings, BrokenReader())
    assert "error" in stats  # swallowed, GrowthOS unaffected
