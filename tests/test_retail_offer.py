import json

from pv_growth.api import webhooks
from pv_growth.database.models import Event, FreeAllocation, Job
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def test_buy_keeps_direct_checkout_and_records_request_not_fake_price_view(session, settings, monkeypatch):
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    t = FakeTelegramTransport()
    client = TelegramClient(t, settings)
    message = webhooks.TgMessage.model_validate(
        {"message_id": 7, "from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "/buy"}
    )
    webhooks._handle_message(client, message)
    webhooks._handle_message(client, message)
    session.expire_all()
    rows = session.query(Event).filter_by(event_type="RETAIL_OFFER_REQUESTED").all()
    assert len(rows) == 1, "a request must be recorded once, not called a click or payment"
    assert rows[0].metadata_json["source"] == "menu"
    sent = [p for method, p in t.calls if method == "sendMessage"]
    assert "https://t.me/pvnetwork_bot" in json.dumps(sent[-1]["reply_markup"])
    assert "اشتراک شخصی" in sent[-1]["text"]
    assert session.query(FreeAllocation).count() == 0 and session.query(Job).count() == 0


def test_public_guide_start_never_runs_audience_gate_and_carries_source(session, settings, monkeypatch):
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)

    def forbidden(*a):
        raise AssertionError("public purchase guidance must not query free eligibility")

    monkeypatch.setattr("pv_growth.free_config.audience.decision_for_user", forbidden)
    t = FakeTelegramTransport()
    message = webhooks.TgMessage.model_validate(
        {
            "message_id": 8,
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
            "text": "/start buy_web",
        }
    )
    webhooks._handle_message(TelegramClient(t, settings), message)
    session.expire_all()
    row = session.query(Event).filter_by(event_type="RETAIL_OFFER_REQUESTED").one()
    assert row.metadata_json["source"] == "web"
    assert session.query(FreeAllocation).count() == 0


def test_intent_callback_is_private_idempotent_and_does_not_claim_a_checkout(session, settings, monkeypatch):
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    t = FakeTelegramTransport()
    client = TelegramClient(t, settings)
    cb = webhooks.TgCallback.model_validate(
        {
            "id": "intent1",
            "from": {"id": 123},
            "message": {"chat": {"id": 123, "type": "private"}},
            "data": "retail:daily",
        }
    )
    webhooks._handle_callback(client, cb)
    webhooks._handle_callback(client, cb)
    session.expire_all()
    assert session.query(Event).filter_by(event_type="RETAIL_NEED_SELECTED").count() == 1
    assert session.query(Event).filter_by(event_type="CHECKOUT_STARTED").count() == 0
    assert "روزمره" in t.sent_texts()[-1]
    cb.message.chat = {"id": -100, "type": "group"}
    webhooks._handle_callback(client, cb)
    assert session.query(Event).filter_by(event_type="RETAIL_NEED_SELECTED").count() == 1


def test_voluntary_share_copy_matches_current_public_and_weekly_policy():
    from urllib.parse import parse_qs, urlparse

    from pv_growth.acquisition.entry import share_url

    text = parse_qs(urlparse(share_url()).query)["text"][0]
    assert "۴۵" not in text and "نداشتن سرویس فعال" not in text
    assert "قابل کپی" in text and "هفت" in text and "۵۰" in text

