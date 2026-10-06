import importlib
import importlib.util

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def module():
    assert importlib.util.find_spec("pv_growth.free_config.membership"), "membership gate missing"
    return importlib.import_module("pv_growth.free_config.membership")


@pytest.mark.parametrize(
    "status,is_member,allowed",
    [
        ("creator", None, True),
        ("administrator", None, True),
        ("member", None, True),
        ("restricted", True, True),
        ("restricted", False, False),
        ("restricted", "true", False),
        ("left", None, False),
        ("kicked", None, False),
        ("unknown", None, False),
    ],
)
def test_membership_reads_both_channels_and_statuses(settings, status, is_member, allowed):
    p = module()
    s = settings.model_copy(update={"free_channel_id": "-1004310246787"})
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": status, "is_member": is_member}
    result = p.membership_for_user(TelegramClient(transport, s), s, 123)
    assert result.eligible is allowed
    assert {call[1]["chat_id"] for call in transport.calls} == {"-1004310246787", "-1003855234264"}
    assert all(method == "getChatMember" for method, _ in transport.calls)


def test_membership_unknown_permission_is_not_missing_membership(settings):
    p = module()
    s = settings.model_copy(update={"free_channel_id": "-1004310246787"})
    transport = FakeTelegramTransport()
    transport.fail_methods.add("getChatMember")
    result = p.membership_for_user(TelegramClient(transport, s), s, 123)
    assert not result.eligible and result.reason == "verification_unavailable"
    assert not result.missing_channels
    text, keyboard = p.join_prompt("shared:2026-10-06:1", reason=result.reason)
    assert "تأیید" in text and "۴۵" in text
    assert keyboard.buttons[0][0]["url"] == "https://t.me/pvnetwork0"
    assert keyboard.buttons[1][0]["url"] == "https://t.me/pvnetwork_freeconfig"
    assert keyboard.buttons[2][0]["callback_data"] == "memberships:shared:2026-10-06:1"


@pytest.mark.parametrize("identity", [0, -123, None])
def test_membership_invalid_identity_has_no_api_call(settings, identity):
    p = module()
    transport = FakeTelegramTransport()
    assert not p.membership_for_user(TelegramClient(transport, settings), settings, identity).eligible
    assert not transport.calls


def test_join_prompt_rejects_unbounded_or_unsafe_context():
    p = module()
    for context in ("bad:<secret>", "community:" + "1" * 100):
        with pytest.raises(ValidationError):
            p.join_prompt(context)


def test_shared_start_requires_both_joins_in_private_chat(session, settings, monkeypatch):
    from pv_growth.api import webhooks
    from pv_growth.free_config import audience
    from pv_growth.free_config.audience import AudienceDecision

    s = settings.model_copy(update={"free_channel_id": "-1004310246787"})
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": "left"}
    webhooks._handle_message(
        TelegramClient(transport, s),
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
                "text": "/start freecfg_shared_2026-10-06_1",
            }
        ),
    )
    text = transport.sent_texts()[0]
    assert "هر دو" in text and "۴۵" in text
    keyboard = next(payload["reply_markup"] for method, payload in transport.calls if method == "sendMessage")
    assert keyboard["inline_keyboard"][2][0]["callback_data"] == "memberships:shared:2026-10-06:1"


def test_group_start_cannot_create_private_identity(session, settings, monkeypatch):
    from pv_growth.api import webhooks
    from pv_growth.database.models import User

    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    transport = FakeTelegramTransport()
    webhooks._handle_message(
        TelegramClient(transport, settings),
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 123},
                "chat": {"id": -123, "type": "supergroup"},
                "text": "/start freecfg_shared_2026-10-06_1",
            }
        ),
    )
    assert not transport.calls and not session.query(User).count()


def test_callback_without_real_private_chat_cannot_create_identity(session, settings, monkeypatch):
    from pv_growth.api import webhooks
    from pv_growth.database.models import User

    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    transport = FakeTelegramTransport()
    webhooks._handle_callback(
        TelegramClient(transport, settings),
        webhooks.TgCallback.model_validate(
            {
                "id": "callback",
                "from": {"id": 123},
                "data": "lottery:pv_daily_lottery",
            }
        ),
    )
    assert not session.query(User).count()
    assert not transport.sent_texts()
