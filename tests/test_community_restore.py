from datetime import date

import pytest

from pv_growth.config_quality.probe import ProbeResult
from pv_growth.core.flags import FlagService
from pv_growth.database.models import (
    AppConfig,
    ConfigSource,
    FreeAllocation,
    MessageLog,
    RawConfig,
    User,
)
from pv_growth.free_config import pipeline
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

URI = "vless://public-example@8.8.8.8:443?security=tls&sni=example.com"


def setup(session, settings, monkeypatch):
    s = settings.model_copy(
        update={
            "env": "production",
            "free_channel_id": "-1004310246787",
            "flag_free_config_enabled": True,
            "flag_public_config_enabled": True,
            "proxy_probe_required": True,
        }
    )
    source = ConfigSource(
        kind="github_file",
        name="public",
        url="https://raw.githubusercontent.com/example/public/main/list.txt",
    )
    session.add(source)
    session.flush()
    row = RawConfig(
        source_id=source.id,
        uri_hash="example-public",
        protocol="vless",
        host="8.8.8.8",
        port=443,
        raw_uri=URI,
        valid=True,
        quality_score=80,
        status="validated",
    )
    session.add_all([row, AppConfig(key="pv_free_growth_policy", value={"community_enabled": True})])
    session.commit()
    monkeypatch.setattr(pipeline, "check_top_candidates", lambda rows, s: {r[0]: True for r in rows})
    monkeypatch.setattr(
        "pv_growth.config_quality.probe.probe_uri", lambda *args: ProbeResult(True, "https_via_proxy", 15)
    )
    t = FakeTelegramTransport()
    t.canned["getChatMember"] = {"status": "member"}
    return s, row, t, TelegramClient(t, s)


def test_production_community_post_is_tested_gated_and_not_owned(session, settings, monkeypatch):
    s, row, t, tg = setup(session, settings, monkeypatch)
    post = pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12))
    assert post is not None, (
        "Owner explicitly restored public web configs; production must honor enabled community policy"
    )
    body = t.sent_texts()[0]
    assert URI not in body, "Raw public config must be delivered only after both memberships"
    assert "متعلق به PV Network نیست" in body
    buttons = [b for r in t.calls[-1][1]["reply_markup"]["inline_keyboard"] for b in r]
    assert any("start=freecfg_community_" in b.get("url", "") for b in buttons)


def test_community_private_delivery_is_real_receipted_and_does_not_mint_pv_budget(
    session, settings, monkeypatch
):
    from pv_growth.free_config import gated_delivery

    s, row, t, tg = setup(session, settings, monkeypatch)
    post = pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12))
    assert post is not None
    user = User(telegram_user_id=123, telegram_chat_id=123)
    session.add(user)
    session.commit()
    monkeypatch.setattr(
        gated_delivery, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    result = gated_delivery.deliver_config(
        session, s, FlagService(s), tg, user_id=user.id, context=f"community:{row.id}"
    )
    assert result == "delivered"
    assert __import__("html").escape(URI) in t.sent_texts()[-1]
    assert session.query(FreeAllocation).count() == 0
    assert session.query(MessageLog).filter_by(status="sent").count() == 1
    again = gated_delivery.deliver_config(
        session, s, FlagService(s), tg, user_id=user.id, context=f"community:{row.id}"
    )
    assert again == "already_delivered"
    assert session.query(MessageLog).count() == 1


def test_community_policy_off_and_failed_https_never_publish(session, settings, monkeypatch):
    s, row, t, tg = setup(session, settings, monkeypatch)
    session.get(AppConfig, "pv_free_growth_policy").value = {"community_enabled": False}
    session.commit()
    assert pipeline.publish_public_slot(session, s, FlagService(s), tg) is None
    session.get(AppConfig, "pv_free_growth_policy").value = {"community_enabled": True}
    session.commit()
    monkeypatch.setattr(
        "pv_growth.config_quality.probe.probe_uri", lambda *args: ProbeResult(False, "https_probe_failed")
    )
    assert pipeline.publish_public_slot(session, s, FlagService(s), tg) is None
    assert not t.sent_texts()


@pytest.mark.parametrize(
    "failure", ["membership", "buyer", "https", "policy_during_probe", "unknown_delivery"]
)
def test_community_delivery_gates_and_unknown_outcomes(session, settings, monkeypatch, failure):
    from pv_growth.core.errors import ValidationError
    from pv_growth.free_config import gated_delivery

    s, row, t, tg = setup(session, settings, monkeypatch)
    assert pipeline.publish_public_slot(session, s, FlagService(s), tg, day=date(2026, 10, 12))
    user = User(telegram_user_id=123, telegram_chat_id=123)
    session.add(user)
    session.commit()
    monkeypatch.setattr(
        gated_delivery, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    if failure == "membership":
        t.canned["getChatMember"] = {"status": "left"}
    if failure == "buyer":
        monkeypatch.setattr(
            gated_delivery, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase")
        )
    if failure == "https":
        monkeypatch.setattr(
            "pv_growth.config_quality.probe.probe_uri", lambda *args: ProbeResult(False, "https_probe_failed")
        )
    if failure == "policy_during_probe":

        def changed(*args):
            session.get(AppConfig, "pv_free_growth_policy").value = {"community_enabled": False}
            session.commit()
            return ProbeResult(True, "https_via_proxy", 15)

        monkeypatch.setattr("pv_growth.config_quality.probe.probe_uri", changed)
    if failure == "unknown_delivery":
        t.fail_methods.add("sendMessage")
    start_calls = len(t.calls)
    if failure in {"membership", "buyer"}:
        with pytest.raises(ValidationError):
            gated_delivery.deliver_config(
                session, s, FlagService(s), tg, user_id=user.id, context=f"community:{row.id}"
            )
    else:
        assert (
            gated_delivery.deliver_config(
                session, s, FlagService(s), tg, user_id=user.id, context=f"community:{row.id}"
            )
            == "unavailable"
        )
    assert not any(m == "sendMessage" for m, _ in t.calls[start_calls:]) or failure == "unknown_delivery"
    assert session.query(FreeAllocation).count() == 0
    if failure == "unknown_delivery":
        assert session.query(MessageLog).one().status == "delivery_unknown"
        before = len(t.calls)
        assert (
            gated_delivery.deliver_config(
                session, s, FlagService(s), tg, user_id=user.id, context=f"community:{row.id}"
            )
            == "already_delivered"
        )
        assert not any(m == "sendMessage" for m, _ in t.calls[before:])
