from pv_growth.api import webhooks
from pv_growth.free_config.audience import AudienceDecision
from tests.test_welcome_gift import setup


def test_welcome_deeplink_and_private_callback_deliver_after_gates(session, settings, monkeypatch):
    p, s, flags, user, backend, telegram, transport, original = setup(session, settings, monkeypatch)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        "pv_growth.free_config.audience.decision_for_user",
        lambda *args: AudienceDecision(True, "never_purchased"),
    )
    monkeypatch.setattr(p, "get_provisioning", lambda *args: backend)
    assert webhooks._free_context("freecfg_welcome_100") == "gift:pv_welcome_100"
    webhooks._handle_message(
        telegram,
        webhooks.TgMessage.model_validate(
            {
                "from": {"id": 1000},
                "chat": {"id": 1000, "type": "private"},
                "text": "/start freecfg_welcome_100",
            }
        ),
    )
    assert "۱۰۰" in transport.sent_texts()[0]
    webhooks._handle_callback(
        telegram,
        webhooks.TgCallback.model_validate(
            {
                "id": "gift",
                "from": {"id": 1000},
                "message": {"chat": {"id": 1000, "type": "private"}},
                "data": "gift:pv_welcome_100",
            }
        ),
    )
    assert len(backend.calls) == 1
    assert "۱۰۰" in transport.sent_texts()[-1]
