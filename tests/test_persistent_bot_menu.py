import pytest

from pv_growth.api import webhooks
from pv_growth.database.models import AppConfig, FreeAllocation, LotteryEntry
from pv_growth.free_config import audience
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient
from tests.test_acquisition_entry import offer, ready


def message(text):
    return webhooks.TgMessage.model_validate(
        {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": text}
    )


@pytest.mark.parametrize("command", ["/free", "/lottery"])
@pytest.mark.parametrize(
    "reason,detail",
    [
        ("active_or_unverified_service", "سرویس فعال یا وضعیت سرویس تأییدنشده"),
        ("recent_purchase", "در ۴۵ روز گذشته خرید"),
        ("history_unavailable", "سابقهٔ خریدت فعلاً قابل تأیید نیست"),
    ],
)
def test_denied_commands_explain_exact_reason_and_keep_visible_navigation(
    session, settings, monkeypatch, command, reason, detail
):
    s = ready(session, settings)
    offer(session, s)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(audience, "decision_for_user", lambda *args: AudienceDecision(False, reason))
    t = FakeTelegramTransport()
    webhooks._handle_message(TelegramClient(t, s), message(command))
    sent = [p for method, p in t.calls if method == "sendMessage"]
    assert detail in sent[-1]["text"]
    assert sent[-1]["reply_markup"]["is_persistent"] is True
    assert session.query(LotteryEntry).count() == 0


def test_start_shows_all_actions_in_persistent_colored_keyboard_even_to_buyer(session, settings, monkeypatch):
    s = ready(session, settings)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase")
    )
    t = FakeTelegramTransport()
    webhooks._handle_message(TelegramClient(t, s), message("/start"))
    markup = [p["reply_markup"] for method, p in t.calls if method == "sendMessage"][-1]
    assert markup["is_persistent"] is True and markup["resize_keyboard"] is True
    buttons = [b for row in markup["keyboard"] for b in row]
    assert len(buttons) == 8
    assert any("قرعه" in b["text"] for b in buttons)
    assert all(b["style"] in {"primary", "success"} for b in buttons)


@pytest.mark.parametrize(
    "text,context", [("🎲 ثبت‌نام قرعه‌کشی", "lottery:pv_daily_lottery"), ("🎁 تست اصلی ۱ گیگی", "shared:")]
)
def test_visible_free_buttons_route_to_existing_gated_dialogue(session, settings, monkeypatch, text, context):
    s = ready(session, settings)
    offer(session, s)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    calls = []
    monkeypatch.setattr(webhooks, "_free_dialogue", lambda *args: calls.append(args[-1]))
    t = FakeTelegramTransport()
    webhooks._handle_message(TelegramClient(t, s), message(text))
    assert calls and calls[0].startswith(context)


@pytest.mark.parametrize("command", ["/web", "🌐 کانفیگ رایگان وب"])
def test_public_web_menu_remains_available_to_buyer_without_private_grant(
    session, settings, monkeypatch, command
):
    s = ready(session, settings)
    session.get(AppConfig, "pv_free_growth_policy").value = {
        "community_enabled": True,
        "community_direct_enabled": True,
        "community_daily_slots": 10,
    }
    session.commit()
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase")
    )
    t = FakeTelegramTransport()
    webhooks._handle_message(TelegramClient(t, s), message(command))
    sent = [p for method, p in t.calls if method == "sendMessage"]
    assert "قابل کپی" in sent[-1]["text"]
    assert "https://t.me/pvnetwork_freeconfig" in str(sent[-1]["reply_markup"])
    assert session.query(FreeAllocation).count() == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("🛒 خرید سرویس اصلی", "خرید"),
        ("📨 معرفی به دوستان", "دوست"),
        ("ℹ️ راهنما", "۲۳"),
        ("🏠 منوی اصلی", "منو"),
        ("🔄 بررسی دوباره شرایط", "سرویس فعال"),
    ],
)
def test_other_visible_menu_actions_have_specific_results(session, settings, monkeypatch, text, expected):
    s = ready(session, settings)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(False, "active_or_unverified_service")
    )
    t = FakeTelegramTransport()
    webhooks._handle_message(TelegramClient(t, s), message(text))
    assert any(expected in p["text"] for method, p in t.calls if method == "sendMessage")
