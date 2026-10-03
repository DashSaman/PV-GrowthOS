"""Mirza read-only MySQL sync: idempotent events, watermark, isolation."""

import uuid

from pv_growth.database.models import AppConfig, Event, User
from pv_growth.mirza_adapter.mysql_reader import (
    WATERMARK_KEY,
    FakeMirzaMySQL,
    sync_mirza,
)


def _invoice(inv_id, tg, status="active", price="990,000", refral=""):
    return {"id_invoice": inv_id, "id_user": tg, "username": f"u{tg}",
            "name_product": "1-month", "price_product": price,
            "Volume": "30", "Service_time": "30", "Status": status,
            "refral": refral}


def test_sync_ingests_idempotently_and_advances_watermark(session, settings):
    tg = int(f"98{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL([_invoice(9001, tg, "active"), _invoice(9002, tg, "unpaid")])

    s1 = sync_mirza(session, settings, reader)
    assert s1["payments"] == 1 and s1["checkouts"] == 1
    assert session.query(Event).filter_by(
        idempotency_key="mirza:pay:9001").count() == 1
    assert session.query(Event).filter_by(
        idempotency_key="mirza:svc:9001").count() == 1
    assert session.query(Event).filter_by(
        idempotency_key="mirza:checkout:9002").count() == 1

    # watermark advanced -> second run sees nothing, no duplicates
    s2 = sync_mirza(session, settings, reader)
    assert s2["fetched"] == 0
    assert session.query(Event).filter(
        Event.idempotency_key.like("mirza:%")).count() == 3
    wm = session.get(AppConfig, WATERMARK_KEY)
    assert wm.value["last_invoice_id"] == 9002

    # user was created via telegram id mapping
    assert session.query(User).filter_by(telegram_user_id=tg).count() == 1


def test_sync_skips_bad_rows_but_advances(session, settings):
    reader = FakeMirzaMySQL([
        _invoice(9101, None, "active"),          # no user -> skipped
        _invoice(9102, 555000111, "weird"),      # unknown status -> skipped
    ])
    stats = sync_mirza(session, settings, reader)
    assert stats["skipped"] == 2 and stats["payments"] == 0
    wm = session.get(AppConfig, WATERMARK_KEY)
    assert wm.value["last_invoice_id"] == 9102


def test_sync_failure_isolated(session, settings):
    class BrokenReader:
        def fetch_invoices_after(self, *a, **k):
            raise RuntimeError("mysql down")

    stats = sync_mirza(session, settings, BrokenReader())
    assert "error" in stats  # swallowed, GrowthOS unaffected
