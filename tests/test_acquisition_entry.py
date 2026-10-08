from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from pv_growth.api import webhooks
from pv_growth.database.models import AppConfig, FreeAllocation, PublishedPost
from pv_growth.database.types import utcnow
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.free_config.budgets import GIB
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def offer(session, settings, *, slot=1, status="published", expires=True, verified=True, receipt=True):
    now = utcnow()
    day = now.date()
    key = f"pv_shared:{day}:{slot}"
    row = FreeAllocation(
        id=key,
        day=day,
        bucket="public",
        traffic_bytes=GIB,
        status=status,
        service_ref=f"growth-example-{slot}",
        expires_at=now + timedelta(hours=1 if expires else -1),
        created_at=now + timedelta(seconds=slot),
        payload={
            "health_method": "https_via_proxy" if verified else "tcp",
            "checked_at_utc": now.isoformat(),
        },
    )
    session.add(row)
    if receipt:
        session.add(
            PublishedPost(
                dedupe_key=key, channel_id=settings.free_channel_id, kind="pv_shared", message_id=slot
            )
        )
    session.commit()
    return row


def ready(session, settings):
    session.add(AppConfig(key="pv_free_growth_policy", value={"public_shared_enabled": True}))
    session.commit()
    return settings.model_copy(
        update={
            "free_channel_id": "-1004310246787",
            "flag_free_config_enabled": True,
            "flag_public_config_enabled": True,
            "flag_pv_exclusive_config_enabled": True,
        }
    )


def test_latest_offer_skips_expired_unknown_unverified_and_missing_receipts(session, settings):
    from pv_growth.acquisition.entry import latest_shared_context

    s = ready(session, settings)
    good = offer(session, s)
    offer(session, s, slot=2, expires=False)
    offer(session, s, slot=3, status="delivery_unknown")
    offer(session, s, slot=4, verified=False)
    offer(session, s, slot=5, receipt=False)
    assert latest_shared_context(session, s) == f"shared:{good.day}:1"


def test_acquisition_start_routes_to_offer_and_attributes_source(session, settings, monkeypatch):
    from pv_growth.database.models import AttributionTouch, Source
    from pv_growth.free_config import audience

    s = ready(session, settings)
    row = offer(session, s)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": "member"}
    webhooks._handle_message(
        TelegramClient(transport, s),
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
                "text": "/start channel_acq_share_20261008",
            }
        ),
    )
    sent = [p for method, p in transport.calls if method == "sendMessage"]
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"shared:{row.day}:1"
    assert session.query(Source).filter_by(code="channel:acq_share_20261008").count() == 1
    assert session.query(AttributionTouch).count() == 2


@pytest.mark.parametrize("reason", ["recent_purchase", "active_service", "history_unavailable"])
def test_acquisition_does_not_offer_free_to_ineligible_user(session, settings, monkeypatch, reason):
    from pv_growth.free_config import audience

    s = ready(session, settings)
    offer(session, s)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(audience, "decision_for_user", lambda *args: AudienceDecision(False, reason))
    t = FakeTelegramTransport()
    webhooks._handle_message(
        TelegramClient(t, s),
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 123},
                "chat": {"id": 123},
                "text": "/start channel_acq_share_20261008",
            }
        ),
    )
    buttons = [
        b
        for method, p in t.calls
        if method == "sendMessage"
        for row in p["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert not any("callback_data" in b or "freeconfig" in b.get("url", "") for b in buttons)
    assert not any(m == "getChatMember" for m, _ in t.calls)


def test_missing_offer_does_not_claim_availability(session, settings, monkeypatch):
    from pv_growth.free_config import audience

    s = ready(session, settings)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    t = FakeTelegramTransport()
    webhooks._handle_message(
        TelegramClient(t, s),
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 123},
                "chat": {"id": 123},
                "text": "/start channel_acq_share_20261008",
            }
        ),
    )
    assert "در دسترس نیست" in t.sent_texts()[0]
    assert not any(m == "getChatMember" for m, _ in t.calls)


def test_share_link_has_traceable_source_and_no_personal_grant_or_reward():
    from pv_growth.acquisition.entry import share_url

    query = parse_qs(urlparse(share_url()).query)
    assert query["url"] == ["https://t.me/pvgrowthos_bot?start=channel_acq_share"]
    assert "pvnetwork_freeconfig" in query["text"][0]
    assert not any(x in query["text"][0] for x in ("vless://", "10%", "۱۰٪", "تضمین", "pvnetwork0"))


def test_share_command_uses_voluntary_share_and_native_referral_entry(session, settings, monkeypatch):
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    t = FakeTelegramTransport()
    webhooks._handle_message(
        TelegramClient(t, settings),
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 123},
                "chat": {"id": 123},
                "text": "/share",
            }
        ),
    )
    assert t.sent_texts()
    buttons = [
        b
        for m, p in t.calls
        if m == "sendMessage"
        for row in p["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert any(b.get("url", "").startswith("https://t.me/share/url?") for b in buttons)
    assert any(b.get("url") == "https://t.me/pvnetwork_bot" and "معرفی" in b["text"] for b in buttons)
