"""Mirza read-only MySQL sync: seen-set scan, idempotent events, transitions,
outage isolation."""

import uuid

import pytest

from pv_growth.database.models import AppConfig, Event, Job, User
from pv_growth.mirza_adapter.mysql_reader import (
    WATERMARK_KEY,
    FakeMirzaMySQL,
    sync_mirza,
)


def _invoice(inv_id, tg, status="active", price="990,000", refral=""):
    return {
        "id_invoice": inv_id,
        "id_user": str(tg),
        "username": f"u{tg}",
        "name_product": "1-month",
        "price_product": price,
        "Volume": "30",
        "Service_time": "30",
        "Status": status,
        "refral": refral,
    }


@pytest.fixture(autouse=True)
def _reset_state(session):
    """The test DB is shared across the session; sync tests start clean."""
    session.query(AppConfig).filter_by(key=WATERMARK_KEY).delete()
    session.commit()


def test_first_scan_ingests_history_once(session, settings):
    """First run ingests the existing stock once (paid + unpaid), then nothing."""
    tg = int(f"98{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL(
        [
            _invoice("aa01", tg, "active"),
            _invoice("aa02", tg, "unpaid"),
            _invoice("aa03", tg, "weirdstatus"),  # unmapped status -> skipped, still seen
        ]
    )
    s1 = sync_mirza(session, settings, reader)
    assert s1["new"] == 3 and s1["payments"] == 1 and s1["checkouts"] == 1
    assert s1["skipped"] == 1

    s2 = sync_mirza(session, settings, reader)  # all seen now
    assert s2["new"] == 0 and s2["payments"] == 0
    assert session.query(Event).filter(Event.idempotency_key.like("mirza:%")).count() == 3
    state = session.get(AppConfig, WATERMARK_KEY)
    assert state.value["count"] == 3
    assert session.query(User).filter_by(telegram_user_id=tg).count() == 1


def test_new_invoice_and_unpaid_to_paid_transition(session, settings):
    tg = int(f"97{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL([_invoice("bb01", tg, "unpaid")])
    first = sync_mirza(session, settings, reader)
    assert first["checkouts"] == 1
    assert session.query(Event).filter_by(idempotency_key="mirza:checkout:bb01").count() == 1

    # the same invoice settles: status flips to active -> PAYMENT_SUCCESS fires
    reader.invoices = [_invoice("bb01", tg, "active")]
    settled = sync_mirza(session, settings, reader)
    assert settled["payments"] == 1
    assert session.query(Event).filter_by(idempotency_key="mirza:pay:bb01").count() == 1
    assert session.query(Event).filter_by(idempotency_key="mirza:svc:bb01").count() == 1

    replay = sync_mirza(session, settings, reader)
    assert replay["payments"] == 0
    keys = [
        key
        for (key,) in session.query(Event.idempotency_key)
        .filter(Event.idempotency_key.like("mirza:%:bb01"))
        .all()
    ]
    assert sorted(keys) == [
        "mirza:checkout:bb01",
        "mirza:pay:bb01",
        "mirza:svc:bb01",
    ]
    conversion_jobs = session.query(Job).filter_by(idempotency_key="conversion:mirza:pay:bb01").all()
    assert len(conversion_jobs) == 1
    assert conversion_jobs[0].payload["order_ref"] == "bb01"
    assert conversion_jobs[0].payload["amount_cents"] == 9_900_000


def test_scan_respects_limit(session, settings):
    invoices = [_invoice(f"cc{i:02d}", 500000000 + i, "active") for i in range(5)]
    reader = FakeMirzaMySQL(invoices)
    s1 = sync_mirza(session, settings, reader, limit=3)
    assert s1["new"] == 3
    s2 = sync_mirza(session, settings, reader, limit=3)
    assert s2["new"] == 2  # picks up the rest on the next cycle


def test_status_transition_limit_does_not_advance_unprocessed_invoice(session, settings):
    tg = int(f"93{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL(
        [
            _invoice("bc01", tg, "unpaid"),
            _invoice("bc02", tg + 1, "unpaid"),
        ]
    )
    sync_mirza(session, settings, reader)

    reader.invoices = [
        _invoice("bc01", tg, "active"),
        _invoice("bc02", tg + 1, "active"),
    ]
    first_settlement = sync_mirza(session, settings, reader, limit=1)
    second_settlement = sync_mirza(session, settings, reader, limit=1)

    assert first_settlement["payments"] == 1
    assert second_settlement["payments"] == 1
    assert (
        session.query(Event).filter(Event.idempotency_key.in_(("mirza:pay:bc01", "mirza:pay:bc02"))).count()
        == 2
    )


def test_sync_failure_isolated(session, settings):
    class BrokenReader:
        def list_invoice_ids(self):
            raise RuntimeError("mysql down")

        def fetch_by_ids(self, ids):
            raise RuntimeError("mysql down")

    stats = sync_mirza(session, settings, BrokenReader())
    assert "error" in stats  # swallowed, GrowthOS unaffected


def test_renewal_emitted_for_repeat_paid_invoice(session, settings):
    tg = int(f"96{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL([_invoice("dd01", tg, "active")])
    sync_mirza(session, settings, reader)  # watermark init + first invoice
    session.commit()

    reader.invoices.append(_invoice("dd02", tg, "active"))  # same user, 2nd paid
    sync_mirza(session, settings, reader)
    # first invoice: PAYMENT+SERVICE only; second: PAYMENT+SERVICE+RENEWED
    assert session.query(Event).filter_by(idempotency_key="mirza:renew:dd01").count() == 0
    assert session.query(Event).filter_by(idempotency_key="mirza:renew:dd02").count() == 1
    # replay-safe
    sync_mirza(session, settings, reader)
    assert session.query(Event).filter(Event.idempotency_key.like("mirza:renew%")).count() == 1


def test_expiry_only_on_observed_transition(session, settings):
    from pv_growth.database.models import AppConfig as Cfg
    from pv_growth.mirza_adapter.mysql_reader import STATUS_MAP_KEY

    tg = int(f"95{uuid.uuid4().int % 10000000}")
    session.query(Cfg).filter(Cfg.key.in_((WATERMARK_KEY, STATUS_MAP_KEY))).delete()
    session.commit()
    reader = FakeMirzaMySQL([_invoice("ee01", tg, "active")])
    sync_mirza(session, settings, reader)  # baseline: active map recorded
    session.commit()
    assert session.query(Event).filter_by(idempotency_key="mirza:exp:ee01").count() == 0

    # Mirza flips the invoice to disabledn -> observed transition -> EXPIRED
    reader.invoices = [_invoice("ee01", tg, "disabledn")]
    stats = sync_mirza(session, settings, reader)
    assert stats.get("expired") == 1
    assert session.query(Event).filter_by(idempotency_key="mirza:exp:ee01").count() == 1

    # a row that was ALREADY disabledn before we watched is never guessed
    reader.invoices.append(_invoice("ee99", tg, "disabledn"))
    stats = sync_mirza(session, settings, reader)
    assert session.query(Event).filter_by(idempotency_key="mirza:exp:ee99").count() == 0


def test_backfill_renewals_matches_paid_counts(session, settings):
    from pv_growth.mirza_adapter.mysql_reader import backfill_renewals

    tg = int(f"94{uuid.uuid4().int % 10000000}")
    reader = FakeMirzaMySQL(
        [
            _invoice("ff01", tg, "active"),
            _invoice("ff02", tg, "active"),
            _invoice("ff03", tg, "active"),  # 3 paid -> 2 renewals expected
        ]
    )
    sync_mirza(session, settings, reader)
    n = (
        session.query(Event)
        .filter(
            Event.event_type == "SERVICE_RENEWED",
            Event.user_id == session.query(User).filter_by(telegram_user_id=tg).one().id,
        )
        .count()
    )
    result = backfill_renewals(session)
    assert result["renewal_events"] >= (2 - n)  # idempotent: fills the gap
    sync_mirza(session, settings, reader)
    uid = session.query(User).filter_by(telegram_user_id=tg).one().id
    renewals = session.query(Event).filter(Event.event_type == "SERVICE_RENEWED", Event.user_id == uid)
    assert renewals.count() == 2  # exactly N-1 for this user
    backfill_renewals(session)  # second run adds nothing
    assert renewals.count() == 2
