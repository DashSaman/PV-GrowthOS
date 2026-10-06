import importlib
import importlib.util
from datetime import UTC, date, datetime, timedelta

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, FreeAllocation, MessageLog, PublishedPost, User
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.free_config.budgets import GIB
from pv_growth.free_config.plan_binding import TUNNEL_INBOUNDS
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

CONTEXT = "shared:2026-10-06:1"
URI = "vless://uuid@8.8.8.8:443?x=<secret>"


def module():
    assert importlib.util.find_spec("pv_growth.free_config.gated_delivery"), "private shared delivery missing"
    return importlib.import_module("pv_growth.free_config.gated_delivery")


def setup(session, settings, monkeypatch):
    p = module()
    s = settings.model_copy(
        update={
            "free_channel_id": "-1004310246787",
            "flag_free_config_enabled": True,
            "flag_public_config_enabled": True,
            "flag_pv_exclusive_config_enabled": True,
        }
    )
    user = User(telegram_user_id=123, telegram_chat_id=123)
    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    stamp = int(expiry.replace(tzinfo=UTC).timestamp() * 1000)
    allocation = FreeAllocation(
        id="pv_shared:2026-10-06:1",
        day=date(2026, 10, 6),
        bucket="public",
        traffic_bytes=GIB,
        status="published",
        service_ref="shared1",
        expires_at=expiry,
        payload={
            "tunnel_plan": {"panel_id": 33, "inbound_ids": list(TUNNEL_INBOUNDS)},
            "service": {
                "service_ref": "shared1",
                "traffic_bytes": GIB,
                "expiry_ts_ms": stamp,
                "client_id": "uuid",
                "sub_id": "test",
            },
            "verified_uri": URI,
        },
    )
    session.add_all(
        [
            user,
            allocation,
            AppConfig(key="pv_free_growth_policy", value={"public_shared_enabled": True}),
            PublishedPost(
                dedupe_key=allocation.id, kind="pv_shared", channel_id=s.free_channel_id, message_id=1
            ),
        ]
    )
    session.commit()
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": "member"}
    telegram = TelegramClient(transport, s)
    monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased"))
    monkeypatch.setattr(p, "verified_uri", lambda *args: (URI, 12))

    class Backend:
        def service_state(self, service_ref):
            return {
                "exists": True,
                "client_id": "uuid",
                "sub_id": "test",
                "enabled": True,
                "expired": False,
                "quota_exhausted": False,
                "traffic_limit_bytes": GIB,
                "traffic_used_bytes": 0,
                "expiry_ts_ms": stamp,
            }

    return p, s, user, allocation, telegram, transport, Backend()


def deliver(p, session, s, user, telegram, backend, context=CONTEXT):
    return p.deliver_config(
        session, s, FlagService(s), telegram, user_id=user.id, context=context, adapter=backend
    )


def test_shared_receipt_private_once_with_durable_log_before_api(session, settings, monkeypatch):
    p, s, user, row, tg, transport, backend = setup(session, settings, monkeypatch)
    original = tg.send_message

    def sending(chat_id, text, keyboard):
        from pv_growth.database.base import session_scope

        with session_scope(s) as fresh:
            assert fresh.query(MessageLog).one().status == "reserved"
        return original(chat_id, text, keyboard)

    monkeypatch.setattr(tg, "send_message", sending)
    assert deliver(p, session, s, user, tg, backend) == "delivered"
    assert deliver(p, session, s, user, tg, backend) == "already_delivered"
    texts = transport.sent_texts()
    assert len(texts) == 1 and "&lt;secret&gt;" in texts[0] and "۱ گیگ مشترک" in texts[0]
    assert session.query(FreeAllocation).count() == 1 and row.traffic_bytes == GIB
    assert session.query(MessageLog).one().status == "sent"
    assert [payload["chat_id"] for method, payload in transport.calls if method == "sendMessage"] == [123]


@pytest.mark.parametrize(
    "stage",
    [
        "membership",
        "identity",
        "buyer",
        "disabled_policy",
        "short_policy",
        "quota",
        "expiry",
        "health",
        "flags",
        "invalid",
        "community",
        "plan",
    ],
)
def test_gated_receipt_fails_closed(session, settings, monkeypatch, stage):
    p, s, user, row, tg, transport, backend = setup(session, settings, monkeypatch)
    context = CONTEXT
    if stage == "membership":
        transport.canned["getChatMember"] = {"status": "left"}
    elif stage == "identity":
        user.telegram_chat_id = -123
        session.commit()
    elif stage in {"buyer", "disabled_policy"}:
        monkeypatch.setattr(
            p,
            "decision_for_user",
            lambda *args: AudienceDecision(
                stage == "disabled_policy",
                "policy_disabled" if stage == "disabled_policy" else "recent_purchase",
            ),
        )
    elif stage == "short_policy":
        session.add(AppConfig(key="free_audience_policy", value={"enabled": True, "dormant_days": 1}))
        session.commit()
    elif stage == "quota":
        monkeypatch.setattr(
            backend,
            "service_state",
            lambda *args: {"exists": True, "enabled": True, "traffic_limit_bytes": 0},
        )
    elif stage == "expiry":
        row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        session.commit()
    elif stage == "health":
        monkeypatch.setattr(p, "verified_uri", lambda *args: (_ for _ in ()).throw(ValidationError("bad")))
    elif stage == "flags":
        s = s.model_copy(update={"flag_public_config_enabled": False})
    elif stage == "invalid":
        context = "shared:2026-10-06:100"
    elif stage == "community":
        context = "community:1"
    elif stage == "plan":
        row.payload = {**row.payload, "tunnel_plan": {"panel_id": 10, "inbound_ids": [1]}}
        session.commit()
    try:
        deliver(p, session, s, user, tg, backend, context)
    except ValidationError:
        pass
    assert not transport.sent_texts() and not session.query(MessageLog).count()


@pytest.mark.parametrize("change", ["membership", "buyer"])
def test_rechecks_after_https_probe_before_receipt(session, settings, monkeypatch, change):
    p, s, user, row, tg, transport, backend = setup(session, settings, monkeypatch)

    def probe(*args):
        if change == "membership":
            transport.canned["getChatMember"] = {"status": "left"}
        else:
            monkeypatch.setattr(
                p, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase")
            )
        return URI, 12

    monkeypatch.setattr(p, "verified_uri", probe)
    with pytest.raises(ValidationError):
        deliver(p, session, s, user, tg, backend)
    assert not transport.sent_texts()


def test_unknown_delivery_is_reserved_and_never_retransmitted(session, settings, monkeypatch):
    p, s, user, row, tg, transport, backend = setup(session, settings, monkeypatch)
    transport.fail_methods.add("sendMessage")
    assert deliver(p, session, s, user, tg, backend) == "unavailable"
    assert session.query(MessageLog).one().status == "delivery_unknown"
    assert deliver(p, session, s, user, tg, backend) == "already_delivered"
    assert len(transport.sent_texts()) == 1
