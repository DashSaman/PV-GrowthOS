import json
from datetime import UTC, datetime, timedelta

import pytest

from pv_growth.api import webhooks
from pv_growth.database.models import AppConfig, Event, FreeAllocation, Job, Source
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient
from tests.test_retail_catalogue import catalogue

CASES = [
    ("retail-outreach:campaign:20261019", "external-acquisition-20261019", "ext261019", 30),
    ("retail-maps:campaign:20261020", "maps-acquisition-20261020", "maps261020", 10),
    ("retail-needs:campaign:20261024", "needs-led-acquisition-20261024", "needs261024", 20),
    ("retail-expand:campaign:20261024", "needs-expansion-20261024", "expand261024", 430),
]


def seed(session, case):
    key, campaign, prefix, limit = case
    route_keys = (
        [f"a{i:02}" for i in range(1, 11)] + [f"s{i:02}" for i in range(1, 21)]
        if limit == 30
        else [
            f"{ {10: 'm', 20: 'n', 430: 'e'}[limit] }{i:03}"
            if limit == 430
            else f"{ {10: 'm', 20: 'n'}[limit] }{i:02}"
            for i in range(1, limit + 1)
        ]
    )
    sources = [
        {
            "key": k,
            "source_code": f"social:{prefix}_{k}",
            "start_parameter": f"social_{prefix}_{k}",
            "segment": ("admin" if k.startswith("a") else "shop") if limit == 30 else "business",
        }
        for k in route_keys
    ]
    now = datetime.now(UTC)
    session.add(
        AppConfig(
            key=key,
            value={
                "stage": "registered",
                "campaign": campaign,
                "owner_authorized_limit": limit,
                "registered_at_utc": (now - timedelta(hours=1)).isoformat(),
                "sources": sources,
                "initial_send_closed": True,
            },
        )
    )
    source = Source(code=sources[0]["source_code"], kind="social", medium="business_email")
    session.add(source)
    current = catalogue()
    current["observed_at_utc"] = now.isoformat()
    session.add(AppConfig(key="retail-catalogue:active", value=current))
    session.commit()
    return sources[0], source


def message(token, mid=71):
    return webhooks.TgMessage.model_validate(
        {
            "message_id": mid,
            "from": {"id": 91001},
            "chat": {"id": 91001, "type": "private"},
            "text": "/start " + token,
        }
    )


@pytest.mark.parametrize("case", CASES)
def test_registered_email_visitor_sees_current_offer_without_free_gate(session, settings, monkeypatch, case):
    route, source = seed(session, case)
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)

    def forbidden(*args):
        raise AssertionError("campaign visitor must reach purchase guidance before free audience lookup")

    monkeypatch.setattr("pv_growth.free_config.audience.decision_for_user", forbidden)
    transport = FakeTelegramTransport()
    client = TelegramClient(transport, settings)
    webhooks._handle_message(client, message(route["start_parameter"]))
    session.expire_all()
    sent = [p for method, p in transport.calls if method == "sendMessage"]
    assert len(sent) == 1
    assert "100,000" in sent[0]["text"] and "تأیید" in sent[0]["text"]
    assert "https://t.me/pvnetwork_bot?start=buy" in json.dumps(sent[0]["reply_markup"])
    assert "منوی اصلی" not in sent[0]["text"]
    event = session.query(Event).filter_by(event_type="SOURCE_ATTRIBUTED").one()
    assert event.source_id == source.id
    assert session.query(Event).filter_by(event_type="RETAIL_OFFER_REQUESTED").count() == 1
    assert session.query(FreeAllocation).count() == 0 and session.query(Job).count() == 0
    webhooks._handle_message(client, message(route["start_parameter"]))
    assert len([p for method, p in transport.calls if method == "sendMessage"]) == 1
    session.expire_all()
    assert session.get(Source, source.id).bot_starts == 1


@pytest.mark.parametrize(
    "change",
    [
        "missing_registry",
        "expired",
        "future",
        "corrupt_routes",
        "missing_source",
        "wrong_source_kind",
        "unregistered_token",
    ],
)
def test_untrusted_or_inactive_route_cannot_use_campaign_entry(session, settings, change):
    from pv_growth.acquisition.campaign_entry import record_entry
    from pv_growth.events.service import get_or_create_user

    route, source = seed(session, CASES[2])
    row = session.get(AppConfig, CASES[2][0])
    token = route["start_parameter"]
    if change == "missing_registry":
        session.delete(row)
    elif change == "missing_source":
        session.delete(source)
    elif change == "wrong_source_kind":
        source.kind = "direct"
    elif change == "unregistered_token":
        token = "social_needs261024_n99"
    else:
        value = dict(row.value)
        if change == "expired":
            value["registered_at_utc"] = (datetime.now(UTC) - timedelta(days=32)).isoformat()
        elif change == "future":
            value["registered_at_utc"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        else:
            value["sources"] = [dict(route, segment="untrusted")]
        row.value = value
    session.commit()
    user, _ = get_or_create_user(session, telegram_user_id=91001)
    assert record_entry(session, user, start_param=token, request_id="m:71") == (False, None)
    assert session.query(Event).filter_by(event_type="RETAIL_OFFER_REQUESTED").count() == 0


def test_expired_catalogue_is_hidden_and_next_need_choice_still_works(session, settings, monkeypatch):
    route, _ = seed(session, CASES[2])
    row = session.get(AppConfig, "retail-catalogue:active")
    row.value = dict(row.value, observed_at_utc=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
    session.commit()
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    transport = FakeTelegramTransport()
    client = TelegramClient(transport, settings)
    webhooks._handle_message(client, message(route["start_parameter"]))
    assert "100,000" not in transport.sent_texts()[0]
    callback = webhooks.TgCallback.model_validate(
        {
            "id": "need1",
            "from": {"id": 91001},
            "message": {"chat": {"id": 91001, "type": "private"}},
            "data": "retail:work",
        }
    )
    webhooks._handle_callback(client, callback)
    session.expire_all()
    event = session.query(Event).filter_by(event_type="RETAIL_NEED_SELECTED").one()
    assert event.metadata_json["source"] == "campaign"
    assert "کار" in transport.sent_texts()[-1]
    assert session.query(FreeAllocation).count() == 0 and session.query(Job).count() == 0


def test_campaign_entry_preserves_existing_first_touch(session, settings, monkeypatch):
    from pv_growth.attribution.service import attribute, first_touch
    from pv_growth.events.service import get_or_create_user

    route, _ = seed(session, CASES[2])
    user, _ = get_or_create_user(session, telegram_user_id=91001)
    original, _ = attribute(session, user.id, "seo_existing")
    session.commit()
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    transport = FakeTelegramTransport()
    webhooks._handle_message(TelegramClient(transport, settings), message(route["start_parameter"]))
    session.expire_all()
    assert first_touch(session, user.id).source_id == original.id
    assert session.query(Event).filter_by(event_type="CHECKOUT_STARTED").count() == 0


def test_expansion_route_must_be_in_actual_registered_subset(session):
    from pv_growth.acquisition.campaign_entry import registered_source

    route, _ = seed(session, CASES[3])
    row = session.get(AppConfig, CASES[3][0])
    row.value = dict(row.value, sources=[route])
    session.add(Source(code="social:expand261024_e002", kind="social", medium="business_email"))
    session.commit()
    assert registered_source(session, "social_expand261024_e002", now=datetime.now(UTC)) is None
    assert registered_source(session, route["start_parameter"], now=datetime.now(UTC)) is not None
