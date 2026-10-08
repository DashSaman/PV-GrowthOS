from datetime import timedelta

from pv_growth.attribution.service import attribute
from pv_growth.database.types import utcnow
from pv_growth.events.service import get_or_create_user, ingest


def test_fixed_source_observations_exclude_before_touch_backfill_proof_and_duplicate_invoice(session):
    from pv_growth.analytics.acquisition import source_observations

    now = utcnow()
    start, end = now - timedelta(hours=6), now
    user, _ = get_or_create_user(session, telegram_user_id=123)
    attribute(session, user.id, "channel_acq_share", occurred_at=start + timedelta(hours=1))
    for key, invoice, offset, kind, amount in [
        ("before", "a", 0, "status_transition", 999),
        ("backfill", "b", 2, "first_observed", 100),
        ("transition", "c", 3, "status_transition", 200),
        ("duplicate", "c", 4, "status_transition", 200),
        ("after", "d", 7, "status_transition", 500),
    ]:
        ingest(
            session,
            "PAYMENT_SUCCESS",
            user_id=user.id,
            idempotency_key=key,
            occurred_at=start + timedelta(hours=offset),
            metadata={
                "source": "mirza_db",
                "mirza_invoice_id": invoice,
                "amount": amount,
                "amount_cents": amount * 10,
                "observation_kind": kind,
            },
        )
    session.flush()
    result = source_observations(session, "channel:acq_share", start=start, end=end)
    assert result["distinct_first_touches"] == 1
    assert result["observed_payers"] == 1 and result["observed_payments"] == 2
    assert result["observed_amount_toman"] == 300
    assert result["status_transition_amount_toman"] == 200
    assert result["first_observed_or_unclassified_payments"] == 1
    assert result["incremental_revenue_proven"] is False
    assert not any("user_id" in k or "telegram" in k for k in result)


def test_sync_marks_first_observed_separately_from_unpaid_to_active(session, settings):
    from pv_growth.database.models import Event
    from pv_growth.mirza_adapter.mysql_reader import FakeMirzaMySQL, sync_mirza

    reader = FakeMirzaMySQL(
        [
            {"id_invoice": "old", "id_user": 123, "Status": "active", "price_product": 100},
            {"id_invoice": "new", "id_user": 124, "Status": "unpaid", "price_product": 200},
        ]
    )
    sync_mirza(session, settings, reader)
    session.flush()
    old = session.query(Event).filter_by(idempotency_key="mirza:pay:old").one()
    assert old.metadata_json["observation_kind"] == "first_observed"
    reader.invoices[1]["Status"] = "active"
    sync_mirza(session, settings, reader)
    session.flush()
    new = session.query(Event).filter_by(idempotency_key="mirza:pay:new").one()
    assert new.metadata_json["observation_kind"] == "status_transition"


def test_http_errors_do_not_export_bot_token(settings, caplog):
    import httpx
    import pytest

    from pv_growth.telegram.client import HttpTelegramTransport, TelegramDeliveryUnknownError

    token = "123:TEST_SECRET_TOKEN"
    transport = HttpTelegramTransport(settings.model_copy(update={"telegram_bot_token": token}))

    class Client:
        def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("timeout at https://api.telegram.org/bot" + token + "/sendMessage")

    transport._client = Client()
    with pytest.raises(TelegramDeliveryUnknownError) as caught:
        transport.call("sendMessage", {"chat_id": 123, "text": "test"})
    assert token not in str(caught.value)
    assert token not in caplog.text
