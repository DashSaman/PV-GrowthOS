from datetime import date, datetime, timedelta

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, Campaign, FreeAllocation, User
from pv_growth.free_config import shared_public
from pv_growth.free_config.budgets import CAPS, MIB
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient
from tests.test_shared_public import Backend


def shared_setup(session, settings, monkeypatch):
    session.add(
        AppConfig(
            key="pv_free_growth_policy",
            value={
                "public_shared_enabled": True,
                "owned_main_direct_enabled": False,
                "owned_shared_direct_enabled": True,
                "owned_shared_channel": "@pvnetwork_freeconfig",
                "weekly_member_gift_enabled": True,
            },
        )
    )
    session.commit()
    s = settings.model_copy(
        update={
            "flag_free_config_enabled": True,
            "flag_public_config_enabled": True,
            "flag_pv_exclusive_config_enabled": True,
            "free_channel_id": "@pvnetwork_freeconfig",
        }
    )
    monkeypatch.setattr(shared_public, "local_day", lambda: date(2026, 10, 12))
    monkeypatch.setattr(shared_public, "verified_uri", lambda *a: ("vless://uuid@8.8.8.8:443#PV", 20))
    transport = FakeTelegramTransport()
    return s, FlagService(s), TelegramClient(transport, s), transport


def test_owned_direct_free_is_scoped_and_does_not_replay(session, settings, monkeypatch):
    s, flags, tg, transport = shared_setup(session, settings, monkeypatch)
    p = shared_public.publish_shared_slot(session, s, flags, tg, Backend(), day=date(2026, 10, 12), slot=7)
    assert p.channel_id == "@pvnetwork_freeconfig"
    sent = [v for method, v in transport.calls if method == "sendMessage"]
    assert len(sent) == 1 and sent[0]["chat_id"] == "@pvnetwork_freeconfig"
    assert "<code>vless://" in sent[0]["text"] and "کیفیت سرویس اصلی" in sent[0]["text"]
    assert "۴۵" not in sent[0]["text"] and "pvnetwork_bot" in sent[0]["text"]
    assert (
        shared_public.publish_shared_slot(session, s, flags, tg, Backend(), day=date(2026, 10, 12), slot=7)
        is None
    )
    with pytest.raises(ValidationError):
        tg.send_channel_post("@pvnetwork0", "Unrelated campaign")
    with pytest.raises(ValidationError):
        tg.send_message("@pvnetwork0", "Unrelated message")


def weekly_setup(session, settings, monkeypatch):
    from pv_growth.free_config import weekly_gift as p

    s = settings.model_copy(
        update={"flag_free_config_enabled": True, "flag_pv_exclusive_config_enabled": True}
    )
    session.add(AppConfig(key="pv_free_growth_policy", value={"weekly_member_gift_enabled": True}))
    session.add(
        Campaign(
            code="pv_weekly_gift",
            name="Member gift",
            kind="free_config_exclusive",
            status="active",
            config={"weekly_member_gift": True},
        )
    )
    user = User(telegram_user_id=12345, telegram_chat_id=12345)
    session.add(user)
    session.commit()
    monkeypatch.setattr(p, "require_membership", lambda *a: None)
    return p, s, FlagService(s), user


def test_weekly_members_including_buyers_and_exact_rolling_limit(session, settings, monkeypatch):
    p, s, flags, user = weekly_setup(session, settings, monkeypatch)
    now = datetime(2026, 10, 12, 12)
    first, new = p.reserve_weekly(session, s, flags, user_id=user.id, now=now)
    assert new and 50 * MIB <= first.traffic_bytes <= 100 * MIB
    assert first.bucket == "lottery" and first.created_at == now
    same, created = p.reserve_weekly(
        session, s, flags, user_id=user.id, now=now + timedelta(days=6, hours=23)
    )
    assert same.id == first.id and not created
    next_, created = p.reserve_weekly(session, s, flags, user_id=user.id, now=now + timedelta(days=7))
    assert created and next_.id != first.id
    assert not hasattr(p, "decision_for_user")


def test_weekly_membership_and_total_budget_fail_closed(session, settings, monkeypatch):
    p, s, flags, user = weekly_setup(session, settings, monkeypatch)
    now = datetime(2026, 10, 12, 12)

    def missing(*args):
        raise ValidationError("join both")

    monkeypatch.setattr(p, "require_membership", missing)
    with pytest.raises(ValidationError):
        p.reserve_weekly(session, s, flags, user_id=user.id, now=now)
    assert not session.query(FreeAllocation).count()
    monkeypatch.setattr(p, "require_membership", lambda *a: None)
    session.add(
        FreeAllocation(
            id="historical",
            day=now.date(),
            bucket="lottery",
            traffic_bytes=CAPS["lottery"] - 49 * MIB,
            status="delivery_unknown",
        )
    )
    session.commit()
    with pytest.raises(ValidationError):
        p.reserve_weekly(session, s, flags, user_id=user.id, now=now)
    assert session.query(FreeAllocation).count() == 1


def test_unknown_weekly_reservation_is_never_a_new_grant(session, settings, monkeypatch):
    p, s, flags, user = weekly_setup(session, settings, monkeypatch)
    now = datetime(2026, 10, 12, 12)
    row, new = p.reserve_weekly(session, s, flags, user_id=user.id, now=now)
    row.status = "delivery_unknown"
    session.commit()
    again, new = p.reserve_weekly(session, s, flags, user_id=user.id, now=now + timedelta(days=1))
    assert again.id == row.id and not new and session.query(FreeAllocation).count() == 1


@pytest.mark.parametrize(
    "command", ["/gift", "/lottery", "🎁 هدیهٔ هفتگی ۵۰–۱۰۰ مگ", "/start freecfg_weekly_member"]
)
def test_buyer_can_open_weekly_dialogue_without_old_purchase_gate(session, settings, monkeypatch, command):
    from pv_growth.api import webhooks
    from pv_growth.free_config import audience
    from pv_growth.free_config.audience import AudienceDecision

    p, s, flags, user = weekly_setup(session, settings, monkeypatch)
    s = s.model_copy(update={"free_channel_id": "@pvnetwork_freeconfig"})
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *a: AudienceDecision(False, "active_or_unverified_service")
    )
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": "member"}
    tg = TelegramClient(transport, s)
    message = webhooks.TgMessage.model_validate(
        {"from": {"id": 12345}, "chat": {"id": 12345, "type": "private"}, "text": command}
    )
    webhooks._handle_message(tg, message)
    text = transport.sent_texts()[-1]
    assert "۵۰ تا ۱۰۰" in text and "حتی خریداران" in text
    assert "۴۵" not in text and "قرعه" not in text


def test_weekly_callback_buyer_reserves_once_and_repeat_has_purchase_cta(session, settings, monkeypatch):
    from pv_growth.api import webhooks
    from pv_growth.free_config import audience

    p, s, flags, user = weekly_setup(session, settings, monkeypatch)
    s = s.model_copy(update={"free_channel_id": "@pvnetwork_freeconfig"})
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience,
        "decision_for_user",
        lambda *a: (_ for _ in ()).throw(AssertionError("buyer gate must not run")),
    )
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": "member"}
    tg = TelegramClient(transport, s)
    callback = webhooks.TgCallback.model_validate(
        {
            "id": "test",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345, "type": "private"}},
            "data": "weekly:pv_weekly_gift",
        }
    )
    webhooks._handle_callback(tg, callback)
    webhooks._handle_callback(tg, callback)
    session.expire_all()
    assert session.query(FreeAllocation).count() == 1
    assert "هفت روز" in transport.sent_texts()[-1] and "pvnetwork_bot" in transport.sent_texts()[-1]


@pytest.mark.parametrize("stage", ["ok", "membership_left", "unknown"])
def test_weekly_delivery_proves_original_quota_and_never_resends(session, settings, monkeypatch, stage):
    from pv_growth.database.models import Event, ExclusiveClaim
    from pv_growth.free_config import weekly_gift
    from tests.test_lottery import delivery_setup

    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)
    campaign = session.get(Campaign, row.campaign_id)
    campaign.code = "pv_weekly_gift"
    campaign.config = {"weekly_member_gift": True}
    session.get(AppConfig, "pv_free_growth_policy").value = {
        "weekly_member_gift_enabled": True,
        "lottery_enabled": False,
    }
    row.payload = {"grant_mode": "weekly_member_gift"}
    row.traffic_bytes = 100 * MIB
    session.get(ExclusiveClaim, row.claim_id).traffic_bytes = 100 * MIB
    session.commit()
    joined = True

    def check(*a):
        if not joined:
            raise ValidationError("left membership")

    monkeypatch.setattr(weekly_gift, "require_membership", check)
    monkeypatch.setattr(
        p, "decision_for_user", lambda *a: (_ for _ in ()).throw(AssertionError("buyer history gate"))
    )
    if stage == "membership_left":

        def probe(*a):
            nonlocal joined
            joined = False
            return "vless://uuid@8.8.8.8:443", 20

        monkeypatch.setattr(p, "verified_uri", probe)
    if stage == "unknown":
        transport.fail_methods.add("sendMessage")
    delivered = p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert delivered == (stage == "ok")
    assert backend.calls[0]["traffic_bytes"] == 100 * MIB and backend.calls[0]["validity_hours"] == 24
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    if stage == "ok":
        assert "هدیهٔ هفتگی" in transport.sent_texts()[0]
        assert (
            session.query(Event).filter_by(event_type="TRIAL_DELIVERED").one().metadata_json["delivery_kind"]
            == "weekly_gift"
        )
    elif stage == "unknown":
        assert row.status == "delivery_unknown"
    else:
        assert row.status == "audience_blocked" and not transport.sent_texts()


def test_weekly_user_lock_prevents_cross_midnight_double_reservation(session, settings, monkeypatch):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL cross-day locking exercised in CI")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from pv_growth.database.base import session_scope

    p, s, flags, user = weekly_setup(session, settings, monkeypatch)
    uid = user.id
    barrier = Barrier(2)

    def attempt(now):
        with session_scope(s) as db:
            barrier.wait(timeout=10)
            row, new = p.reserve_weekly(db, s, flags, user_id=uid, now=now)
            db.commit()
            return row.id, new

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attempt, t) for t in (datetime(2026, 10, 12, 20, 29), datetime(2026, 10, 12, 20, 31))
        ]
        result = [f.result(timeout=30) for f in futures]
    assert len({r[0] for r in result}) == 1 and sum(r[1] for r in result) == 1


def test_weekly_campaign_cannot_use_generic_unlimited_claim(session, settings, monkeypatch):
    from pv_growth.free_config.exclusive import FakeProvisioningClient, claim_exclusive

    p, s, flags, user = weekly_setup(session, settings, monkeypatch)
    with pytest.raises(ValidationError):
        claim_exclusive(
            session, s, flags, FakeProvisioningClient(), campaign_code="pv_weekly_gift", user_id=user.id
        )
    assert not session.query(FreeAllocation).count()


@pytest.mark.parametrize("destination", ["@pvnetwork0", "pvnetwork0", "-1003855234264"])
def test_stale_main_policy_cannot_publish_or_allocate(session, settings, monkeypatch, destination):
    s, flags, tg, transport = shared_setup(session, settings, monkeypatch)
    row = session.get(AppConfig, "pv_free_growth_policy")
    row.value = {**row.value, "owned_main_direct_enabled": True, "owned_shared_channel": destination}
    session.commit()
    assert (
        shared_public.publish_shared_slot(session, s, flags, tg, Backend(), day=date(2026, 10, 12), slot=8)
        is None
    )
    assert not session.query(FreeAllocation).count()
    assert not transport.calls


def test_destination_change_during_probe_aborts_before_send(session, settings, monkeypatch):
    s, flags, tg, transport = shared_setup(session, settings, monkeypatch)

    def probe(*args):
        row = session.get(AppConfig, "pv_free_growth_policy")
        row.value = {**row.value, "owned_shared_channel": "@pvnetwork0"}
        session.commit()
        return "vless://uuid@8.8.8.8:443#PV", 20

    monkeypatch.setattr(shared_public, "verified_uri", probe)
    assert (
        shared_public.publish_shared_slot(session, s, flags, tg, Backend(), day=date(2026, 10, 12), slot=8)
        is None
    )
    assert not transport.calls
