"""Buyer exclusion is checked against original orders AND later purchases."""

from datetime import UTC, datetime, timedelta

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, Campaign, Event, ExclusiveClaim
from pv_growth.events.service import get_or_create_user
from pv_growth.free_config.exclusive import FakeProvisioningClient, claim_exclusive

NOW = datetime(2026, 10, 6, 13, tzinfo=UTC)


def invoice(days=46, status="disabledn", price="100000"):
    return {
        "Status": status,
        "price_product": price,
        "time_sell": str(int((NOW - timedelta(days=days)).timestamp())),
    }


@pytest.mark.parametrize(
    "orders,other,payments,eligible",
    [
        ([], [], [], True),
        ([invoice(45)], [], [], True),
        ([invoice(44)], [], [], False),
        ([invoice(90, "active")], [], [], False),
        (
            [invoice(90)],
            [{"type": "extend_user", "status": "paid", "price": "100000", "time": "2026/10/05 16:00:00"}],
            [],
            False,
        ),
        ([], [], [{"payment_Status": "paid", "price": "100000", "time": "2026/10/05 16:00:00"}], False),
        ([], [{"type": "extend_user", "status": "unpaid", "price": "100000", "time": "bad"}], [], True),
        ([{**invoice(), "time_sell": "bad"}], [], [], False),
        ([invoice(status="unknown")], [], [], False),
        ([invoice(price="bad")], [], [], False),
        (
            [],
            [
                {
                    "type": "new_unrecognized_operation",
                    "status": "",
                    "price": "0",
                    "time": "2026/01/01 00:00:00",
                }
            ],
            [],
            False,
        ),
    ],
)
def test_authoritative_purchase_history(orders, other, payments, eligible):
    from pv_growth.free_config.audience import evaluate_history

    assert evaluate_history(orders, other, payments, now=NOW, dormant_days=45).eligible is eligible


def enable_policy(session):
    session.add(AppConfig(key="free_audience_policy", value={"enabled": True, "dormant_days": 45}))
    session.flush()


def test_recent_buyer_cannot_create_or_replay_free_claim(session, settings, monkeypatch):
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    enable_policy(session)
    monkeypatch.setattr(
        MirzaMySQLReader,
        "fetch_free_audience_history",
        lambda self, tg: ([invoice(1)], [], []),
        raising=False,
    )
    user, _ = get_or_create_user(session, telegram_user_id=85001)
    campaign = Campaign(
        code="audience", name="Audience", kind="free_config_exclusive", status="active", config={}
    )
    session.add(campaign)
    session.flush()
    provisioning = FakeProvisioningClient()
    flags = FlagService(
        settings.model_copy(
            update={"flag_free_config_enabled": True, "flag_pv_exclusive_config_enabled": True}
        )
    )
    with pytest.raises(ValidationError):
        claim_exclusive(session, settings, flags, provisioning, campaign_code="audience", user_id=user.id)
    assert provisioning.created == []
    assert session.query(ExclusiveClaim).count() == 0
    assert session.query(Event).filter_by(event_type="TRIAL_CREATED").count() == 0


def test_history_read_failure_never_qualifies(session, settings, monkeypatch):
    from pv_growth.free_config.audience import decision_for_user
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    enable_policy(session)
    user, _ = get_or_create_user(session, telegram_user_id=85002)

    def unavailable(self, tg):
        raise RuntimeError("read denied: sensitive details must not reach decision")

    monkeypatch.setattr(MirzaMySQLReader, "fetch_free_audience_history", unavailable)
    result = decision_for_user(session, settings, user.id)
    assert result.eligible is False
    assert result.reason == "history_unavailable"


def test_retry_after_purchase_is_terminal_without_panel_call(session, settings, monkeypatch):
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
    from pv_growth.provisioning.lifecycle_job import retry_pending_claim

    enable_policy(session)
    monkeypatch.setattr(
        MirzaMySQLReader, "fetch_free_audience_history", lambda self, tg: ([invoice(1)], [], [])
    )
    user, _ = get_or_create_user(session, telegram_user_id=85003)
    campaign = Campaign(
        code="retry-audience", name="Audience", kind="free_config_exclusive", status="active", config={}
    )
    session.add(campaign)
    session.flush()
    claim = ExclusiveClaim(
        claim_key="retry-buyer",
        campaign_id=campaign.id,
        user_id=user.id,
        claim_date=NOW.date(),
        status="pending_provision",
        config_payload={},
    )
    session.add(claim)
    session.flush()
    panel_calls = []
    monkeypatch.setattr(
        "pv_growth.provisioning.lifecycle_job.get_provisioning", lambda s: panel_calls.append(s)
    )
    retry_pending_claim(session, settings, {"claim_id": claim.id})
    assert claim.status == "audience_blocked"
    assert panel_calls == []
    assert session.query(Event).filter_by(event_type="TRIAL_CREATED").count() == 0


def test_production_enforces_policy_without_config_row(session, settings, monkeypatch):
    from pv_growth.free_config.audience import decision_for_user
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    user, _ = get_or_create_user(session, telegram_user_id=85004)
    monkeypatch.setattr(
        MirzaMySQLReader, "fetch_free_audience_history", lambda self, tg: ([invoice(1)], [], [])
    )
    assert not decision_for_user(session, settings.model_copy(update={"env": "production"}), user.id).eligible


@pytest.mark.parametrize("policy", [{"enabled": False}, {"enabled": True, "dormant_days": 44}])
def test_production_policy_cannot_relax_owner_45_day_rule(session, settings, policy, monkeypatch):
    from pv_growth.free_config.audience import decision_for_user
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    monkeypatch.setattr(MirzaMySQLReader, "fetch_free_audience_history", lambda *a: ([], [], []))
    user, _ = get_or_create_user(session, telegram_user_id=85008)
    session.add(AppConfig(key="free_audience_policy", value=policy))
    session.flush()
    assert not decision_for_user(session, settings.model_copy(update={"env": "production"}), user.id).eligible


def test_read_queries_are_select_only_and_parameterized(settings):
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, parameters):
            statements.append((query, parameters))

        def fetchall(self):
            return []

    class Connection(Cursor):
        def cursor(self):
            return Cursor()

    reader = MirzaMySQLReader(settings)
    reader.connect = lambda: Connection()
    assert reader.fetch_free_audience_history(85005) == ([], [], [])
    assert len(statements) == 3
    assert all(sql.startswith("SELECT ") and args == (85005,) for sql, args in statements)


def test_purchase_after_invite_queue_stops_delivery(session, settings, monkeypatch):
    from pv_growth.database.models import LifecycleRule, MessageLog, MessageTemplate
    from pv_growth.lifecycle.service import send_followup
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
    from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

    enable_policy(session)
    user, _ = get_or_create_user(session, telegram_user_id=85006, telegram_chat_id=85006)
    session.add(MessageTemplate(code="free-invite", intent="acquisition", body="دعوت رایگان"))
    session.add(
        LifecycleRule(
            code="free-invite",
            trigger="started_no_trial",
            template_code="free-invite",
            conditions={"free_audience": True},
            stop_conditions=[],
        )
    )
    session.flush()
    monkeypatch.setattr(
        MirzaMySQLReader, "fetch_free_audience_history", lambda self, tg: ([invoice(1)], [], [])
    )
    telegram = TelegramClient(FakeTelegramTransport(), settings)
    flags = FlagService(settings.model_copy(update={"flag_lifecycle_automation_enabled": True}))
    assert (
        send_followup(session, settings, flags, telegram, {"rule_code": "free-invite", "user_id": user.id})
        == "stopped"
    )
    assert telegram._t.calls == []
    assert session.query(MessageLog).count() == 0


@pytest.mark.parametrize("eligible", [True, False])
def test_start_offer_checks_audience_and_keeps_complete_campaign_code(
    session, settings, monkeypatch, eligible
):
    from pv_growth.api import webhooks
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
    from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

    enable_policy(session)
    session.commit()
    configured = settings.model_copy(update={"free_channel_id": "@pvnetwork_freeconfig"})
    monkeypatch.setattr(webhooks, "get_settings", lambda: configured)
    history = ([], [], []) if eligible else ([invoice(1)], [], [])
    monkeypatch.setattr(MirzaMySQLReader, "fetch_free_audience_history", lambda self, tg: history)
    fake = FakeTelegramTransport()
    fake.canned["getChatMember"] = {"status": "member"}
    telegram = TelegramClient(fake, configured)
    message = webhooks.TgMessage.model_validate(
        {
            "message_id": 1,
            "chat": {"id": 85007},
            "from": {"id": 85007},
            "text": "/start freecfg_complete_campaign",
        }
    )
    webhooks._handle_message(telegram, message)
    payload = next(payload for method, payload in telegram._t.calls if method == "sendMessage")
    buttons = [button for row in payload["reply_markup"]["inline_keyboard"] for button in row]
    if eligible:
        assert buttons[0]["callback_data"] == "claim:complete_campaign"
    else:
        assert buttons == [{"text": "🛒 خرید و تعرفه‌ها", "url": "https://t.me/pvnetwork_bot"}]
        assert "تست رایگان" not in payload["text"]
