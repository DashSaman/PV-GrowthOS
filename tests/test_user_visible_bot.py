"""Regression feedback for the user's silent private-bot journeys."""

import pytest

from pv_growth.api import webhooks
from pv_growth.free_config import audience
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient
from tests.test_acquisition_entry import offer, ready
from tests.test_lottery import setup as lottery_setup


@pytest.mark.parametrize("command", ["/start", "/help", "سلام"])
@pytest.mark.parametrize(
    ("reason", "explanation"),
    [
        ("active_or_unverified_service", "سرویس فعال یا وضعیت سرویس تأییدنشده"),
        ("recent_purchase", "در ۴۵ روز گذشته خرید"),
        ("history_unavailable", "سابقهٔ خریدت فعلاً قابل تأیید نیست"),
    ],
)
def test_ineligible_home_explains_reason_without_offering_grants(
    session, settings, monkeypatch, command, reason, explanation
):
    s = ready(session, settings)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(audience, "decision_for_user", lambda *args: AudienceDecision(False, reason))
    t = FakeTelegramTransport()
    webhooks._handle_message(
        TelegramClient(t, s),
        webhooks.TgMessage.model_validate(
            {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": command}
        ),
    )
    assert any(explanation in text for text in t.sent_texts())
    callbacks = [
        b.get("callback_data", "")
        for m, p in t.calls
        if m == "sendMessage"
        for r in p.get("reply_markup", {}).get("inline_keyboard", [])
        for b in r
    ]
    assert not any(c.startswith(("shared:", "community:", "lottery:")) for c in callbacks)


def test_bare_start_exposes_current_test_and_lottery_directly(session, settings, monkeypatch):
    s = ready(session, settings)
    row = offer(session, s)
    from pv_growth.database.models import AppConfig

    session.get(AppConfig, "pv_free_growth_policy").value = {
        "public_shared_enabled": True,
        "lottery_enabled": True,
    }
    session.commit()
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    t = FakeTelegramTransport()
    t.canned["getChatMember"] = {"status": "member"}
    webhooks._handle_message(
        TelegramClient(t, s),
        webhooks.TgMessage.model_validate(
            {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "/start"}
        ),
    )
    buttons = [
        b for m, p in t.calls if m == "sendMessage" for r in p["reply_markup"]["inline_keyboard"] for b in r
    ]
    callbacks = [b.get("callback_data") for b in buttons]
    assert f"shared:{row.day}:1" in callbacks, "Opening the bot must expose actual config retrieval"
    assert "lottery:pv_daily_lottery" in callbacks, "Opening the bot must expose lottery registration"


def test_lottery_success_leaves_persistent_private_receipt(session, settings, monkeypatch):
    from pv_growth.database.models import LotteryEntry

    _, s, _, _, users = lottery_setup(session, settings, monkeypatch)
    s = s.model_copy(update={"free_channel_id": "-1004310246787"})
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    t = FakeTelegramTransport()
    t.canned["getChatMember"] = {"status": "member"}
    user = users[0]
    webhooks._handle_callback(
        TelegramClient(t, s),
        webhooks.TgCallback.model_validate(
            {
                "id": "registration",
                "from": {"id": user.telegram_user_id},
                "message": {"chat": {"id": user.telegram_user_id, "type": "private"}},
                "data": "lottery:pv_daily_lottery",
            }
        ),
    )
    assert session.query(LotteryEntry).count() == 1
    assert any("ثبت" in text and "۲۳" in text for text in t.sent_texts()), (
        "A popup alone is not a visible receipt"
    )


def test_ineligible_claim_explains_denial_in_private_chat(session, settings, monkeypatch):
    s = ready(session, settings)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase")
    )
    t = FakeTelegramTransport()
    webhooks._handle_callback(
        TelegramClient(t, s),
        webhooks.TgCallback.model_validate(
            {
                "id": "claim",
                "from": {"id": 123},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "shared:2026-10-12:1",
            }
        ),
    )
    assert t.sent_texts(), "Rejected requests must not appear to do nothing"
    assert not any("config_uri" in p for _, p in t.calls)


def test_ineligible_deeplink_explains_why_no_config_is_offered(session, settings, monkeypatch):
    s = ready(session, settings)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase")
    )
    t = FakeTelegramTransport()
    webhooks._handle_message(
        TelegramClient(t, s),
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
                "text": "/start freecfg_shared_2026-10-12_1",
            }
        ),
    )
    assert any("۴۵" in text for text in t.sent_texts()), (
        "Explain the eligibility rule, not just an unrelated ad"
    )


def test_free_and_lottery_commands_are_not_silently_ignored(session, settings, monkeypatch):
    s = ready(session, settings)
    offer(session, s)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    for command in ("/free", "/lottery"):
        t = FakeTelegramTransport()
        t.canned["getChatMember"] = {"status": "member"}
        webhooks._handle_message(
            TelegramClient(t, s),
            webhooks.TgMessage.model_validate(
                {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": command}
            ),
        )
        assert t.sent_texts(), f"{command} must provide a usable next action"


def test_callback_is_acknowledged_before_slow_history_checks(session, settings, monkeypatch):
    s = ready(session, settings)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    t = FakeTelegramTransport()

    def history(*args):
        assert t.calls and t.calls[0][0] == "answerCallbackQuery", (
            "Stop the spinner before network/history work"
        )
        return AudienceDecision(False, "recent_purchase")

    monkeypatch.setattr(audience, "decision_for_user", history)
    webhooks._handle_callback(
        TelegramClient(t, s),
        webhooks.TgCallback.model_validate(
            {
                "id": "press",
                "from": {"id": 123},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "shared:2026-10-12:1",
            }
        ),
    )
    assert t.calls[0][0] == "answerCallbackQuery", (
        "Stop the spinner before history checks and private delivery"
    )
    assert t.sent_texts()
