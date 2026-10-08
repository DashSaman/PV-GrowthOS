import importlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import (
    AppConfig,
    Campaign,
    ExclusiveClaim,
    FreeAllocation,
    LotteryDraw,
    LotteryEntry,
    User,
)
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.free_config.budgets import CAPS, GIB, MIB, reserve
from pv_growth.free_config.exclusive import FakeProvisioningClient, claim_exclusive
from pv_growth.provisioning.xui import client_email_for
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

DAY = date(2026, 10, 6)
BEFORE = datetime(2026, 10, 6, 19, 29)
CUTOFF = datetime(2026, 10, 6, 19, 30)


def module():
    assert importlib.util.find_spec("pv_growth.free_config.lottery"), "opt-in lottery subsystem missing"
    return importlib.import_module("pv_growth.free_config.lottery")


def setup(session, settings, monkeypatch, count=1):
    p = module()
    s = settings.model_copy(
        update={"flag_free_config_enabled": True, "flag_pv_exclusive_config_enabled": True}
    )
    session.add(AppConfig(key="pv_free_growth_policy", value={"lottery_enabled": True}))
    campaign = Campaign(
        code="pv_daily_lottery",
        name="Lottery",
        kind="free_config_exclusive",
        status="active",
        config={"lottery": True},
    )
    session.add(campaign)
    users = [User(telegram_user_id=1000 + n, telegram_chat_id=1000 + n) for n in range(count)]
    session.add_all(users)
    session.commit()
    monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased"))
    monkeypatch.setattr(p, "require_membership", lambda *args, **kwargs: None)
    monkeypatch.setattr(p, "utcnow", lambda: CUTOFF)
    return p, s, FlagService(s), campaign, users


def test_lottery_rejects_missing_membership_at_entry_and_draw(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch)
    joined = True

    def check(*args, **kwargs):
        if not joined:
            raise ValidationError("join both channels")

    monkeypatch.setattr(p, "require_membership", check, raising=False)
    enter(p, session, s, flags, users[0])
    joined = False
    with pytest.raises(ValidationError):
        enter(p, session, s, flags, users[0])
    frozen = draw(p, session, s, flags)
    assert frozen.meta["winners"] == 0 and not session.query(ExclusiveClaim).count()


def test_lottery_rechecks_membership_after_https_before_delivery(session, settings, monkeypatch):
    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)
    joined = True

    def check(*args, **kwargs):
        if not joined:
            raise ValidationError("left channel during probe")

    def probe(*args):
        nonlocal joined
        joined = False
        return "vless://uuid@8.8.8.8:443", 20

    monkeypatch.setattr(p, "require_membership", check, raising=False)
    monkeypatch.setattr(p, "verified_uri", probe)
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert not transport.sent_texts() and row.status == "audience_blocked"


def enter(p, session, s, flags, user, now=BEFORE):
    return p.enter_lottery(session, s, flags, campaign_code="pv_daily_lottery", user_id=user.id, now=now)


def draw(p, session, s, flags):
    return p.freeze_draw(session, s, flags, campaign_code="pv_daily_lottery", day=DAY, now=CUTOFF)


def test_lottery_campaign_cannot_bypass_draw_with_claim(session, settings, monkeypatch):
    s = settings.model_copy(
        update={"flag_free_config_enabled": True, "flag_pv_exclusive_config_enabled": True}
    )
    campaign = Campaign(
        code="pv_daily_lottery",
        name="Lottery",
        kind="free_config_exclusive",
        status="active",
        config={"lottery": True},
    )
    user = User(telegram_user_id=123)
    session.add_all([campaign, user])
    session.commit()
    from pv_growth.free_config import audience

    monkeypatch.setattr(audience, "require_free_audience", lambda *args: None)
    with pytest.raises(ValidationError, match="lottery"):
        claim_exclusive(
            session, s, FlagService(s), FakeProvisioningClient(), campaign_code=campaign.code, user_id=user.id
        )
    assert not session.scalars(select(ExclusiveClaim)).all()


def test_entry_once_and_cutoff_routes_next_day(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch)
    first, new = enter(p, session, s, flags, users[0])
    again, repeated = enter(p, session, s, flags, users[0])
    late, late_new = enter(p, session, s, flags, users[0], CUTOFF)
    assert new and not repeated and again.id == first.id
    assert first.draw_date == DAY and late.draw_date == DAY + timedelta(days=1) and late_new
    assert session.query(LotteryEntry).count() == 2


def test_entry_fails_closed_without_audience_or_policy(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch)
    monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(False, "history_unavailable"))
    with pytest.raises(ValidationError):
        enter(p, session, s, flags, users[0])
    monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased"))
    session.get(AppConfig, "pv_free_growth_policy").value = {"lottery_enabled": False}
    with pytest.raises(ValidationError):
        enter(p, session, s, flags, users[0])
    assert session.query(LotteryEntry).count() == 0


@pytest.mark.parametrize("reason,days", [("policy_disabled", 45), ("dormant", 1)])
def test_lottery_requires_real_history_and_at_least_45_day_exclusion(
    session, settings, monkeypatch, reason, days
):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch)
    session.add(AppConfig(key="free_audience_policy", value={"enabled": True, "dormant_days": days}))
    session.commit()
    monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(True, reason))
    with pytest.raises(ValidationError):
        enter(p, session, s, flags, users[0])
    assert session.query(LotteryEntry).count() == 0


def test_frozen_draw_reserves_real_entrants_once_and_one_win(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch, 3)
    for user in users:
        enter(p, session, s, flags, user)
    with pytest.raises(ValidationError):
        p.freeze_draw(session, s, flags, campaign_code=campaign.code, day=DAY, now=BEFORE)
    first = draw(p, session, s, flags)
    original = [(r.id, r.traffic_bytes) for r in session.query(FreeAllocation).all()]
    assert draw(p, session, s, flags).id == first.id
    assert original == [(r.id, r.traffic_bytes) for r in session.query(FreeAllocation).all()]
    assert len(original) == 3 and sum(q for _, q in original) <= 3 * GIB
    assert all(50 * MIB <= q <= GIB for _, q in original)
    assert all(
        c.traffic_bytes and c.validity_hours == 24 and c.status == "lottery_pending"
        for c in session.query(ExclusiveClaim)
    )
    with pytest.raises(ValidationError):
        enter(p, session, s, flags, users[0], CUTOFF)


def test_draw_rechecks_buyers_and_honors_remaining_budget(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch, 3)
    for user in users:
        enter(p, session, s, flags, user)
    for n in range(49):
        reserve(session, key=f"previous:{n}", day=DAY, bucket="lottery", traffic_bytes=GIB)
    reserve(session, key="partial", day=DAY, bucket="lottery", traffic_bytes=GIB - 50 * MIB)
    buyer = users[0].id
    monkeypatch.setattr(
        p,
        "decision_for_user",
        lambda sess, conf, uid: AudienceDecision(
            uid != buyer, "recent_purchase" if uid == buyer else "never_purchased"
        ),
    )
    draw(p, session, s, flags)
    claims = session.query(ExclusiveClaim).all()
    assert len(claims) == 1 and claims[0].user_id != buyer and claims[0].traffic_bytes == 50 * MIB
    assert sum(row.traffic_bytes for row in session.query(FreeAllocation)) == CAPS["lottery"]


class Backend:
    def __init__(self):
        self.calls = []
        self.service = None

    def health(self):
        return True

    def create_temp_service(self, **kwargs):
        self.calls.append(kwargs)
        self.service = {
            "client_id": "uuid",
            "sub_id": "test",
            "service_ref": client_email_for(kwargs["idempotency_key"]),
            "traffic_bytes": kwargs["traffic_bytes"],
            "expiry_ts_ms": int((CUTOFF + timedelta(hours=24)).replace(tzinfo=UTC).timestamp() * 1000),
            "config_uri": "https://sub.example/link/test",
        }
        return self.service

    def service_state(self, ref):
        return {
            "exists": True,
            "client_id": "uuid",
            "sub_id": "test",
            "enabled": True,
            "traffic_limit_bytes": self.service["traffic_bytes"],
            "traffic_used_bytes": 0,
            "expiry_ts_ms": self.service["expiry_ts_ms"],
        }


def delivery_setup(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch)
    enter(p, session, s, flags, users[0])
    draw(p, session, s, flags)
    transport = FakeTelegramTransport()
    monkeypatch.setattr(p, "verified_uri", lambda service, conf: ("vless://uuid@8.8.8.8:443?x=<secret>", 20))
    return (
        p,
        s,
        flags,
        session.query(FreeAllocation).one(),
        Backend(),
        TelegramClient(transport, s),
        transport,
    )


def test_delivery_exact_quota_actual_proof_and_private_single_receipt(session, settings, monkeypatch):
    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)
    assert p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    claim = session.get(ExclusiveClaim, row.claim_id)
    assert claim.status == "active" and row.status == "delivered"
    assert backend.calls[0]["traffic_bytes"] == row.traffic_bytes and backend.calls[0]["validity_hours"] == 24
    texts = transport.sent_texts()
    assert len(texts) == 1 and "&lt;secret&gt;" in texts[0]
    assert transport.calls[0][1]["chat_id"] > 0
    from pv_growth.database.models import Event

    assert session.query(Event).filter_by(event_type="TRIAL_DELIVERED").count() == 1


@pytest.mark.parametrize("stage", ["eligibility", "quota", "health", "unknown"])
def test_delivery_fails_closed_and_retains_budget(session, settings, monkeypatch, stage):
    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)
    if stage == "eligibility":
        monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase"))
    elif stage == "quota":
        monkeypatch.setattr(
            backend, "service_state", lambda ref: {"exists": True, "enabled": True, "traffic_limit_bytes": 0}
        )
    elif stage == "health":
        monkeypatch.setattr(
            p, "verified_uri", lambda *args: (_ for _ in ()).throw(ValidationError("bad actual HTTPS"))
        )
    else:
        transport.fail_methods.add("sendMessage")
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert row.traffic_bytes > 0 and session.query(FreeAllocation).count() == 1
    from pv_growth.database.models import Event

    assert session.query(Event).filter_by(event_type="TRIAL_DELIVERED").count() == 0
    if stage == "unknown":
        assert row.status == "delivery_unknown"
        assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
        assert len(transport.calls) == 1
    else:
        assert not transport.calls


def test_start_lottery_explains_draw_and_callback_enters(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch)
    s = s.model_copy(update={"free_channel_id": "-1004310246787"})
    from pv_growth.api import webhooks
    from pv_growth.free_config import audience

    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(
        audience, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased")
    )
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": "member"}
    telegram = TelegramClient(transport, s)
    webhooks._handle_message(
        telegram,
        webhooks.TgMessage.model_validate(
            {"from": {"id": 1000}, "chat": {"id": 1000}, "text": "/start freecfg_pv_daily_lottery"}
        ),
    )
    text = transport.sent_texts()[0]
    assert "قرعه" in text and "۲۳" in text
    keyboard = next(
        payload["reply_markup"]["inline_keyboard"]
        for method, payload in transport.calls
        if method == "sendMessage"
    )
    assert keyboard[0][0]["callback_data"] == "lottery:pv_daily_lottery"
    webhooks._handle_callback(
        telegram,
        webhooks.TgCallback.model_validate(
            {
                "id": "callback",
                "from": {"id": 1000},
                "data": "lottery:pv_daily_lottery",
                "message": {"chat": {"id": 1000, "type": "private"}},
            }
        ),
    )
    assert session.query(LotteryEntry).count() == 1
    assert not session.query(ExclusiveClaim).count()


def test_daily_global_maximum_winners_applies_across_campaign_reservations(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch, 2)
    for n in range(100):
        reserve(session, key=f"reserved:{n}", day=DAY, bucket="lottery", traffic_bytes=50 * MIB)
    for user in users:
        enter(p, session, s, flags, user)
    frozen = draw(p, session, s, flags)
    assert frozen.meta["winners"] == 0
    assert session.query(ExclusiveClaim).count() == 0


def test_delivery_rechecks_after_actual_https_before_message(session, settings, monkeypatch):
    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)
    eligible = True
    monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(eligible, "recent_purchase"))

    def purchase_during_health(*args):
        nonlocal eligible
        eligible = False
        return "vless://uuid@8.8.8.8:443", 20

    monkeypatch.setattr(p, "verified_uri", purchase_during_health)
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert row.status == "audience_blocked" and not transport.calls


@pytest.mark.parametrize(
    "stamp", [0, int((CUTOFF + timedelta(hours=25)).replace(tzinfo=UTC).timestamp() * 1000)]
)
def test_panel_missing_or_extended_expiry_prevents_delivery(session, settings, monkeypatch, stamp):
    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)
    original = backend.create_temp_service

    def altered(**kwargs):
        service = original(**kwargs)
        service["expiry_ts_ms"] = stamp
        return service

    monkeypatch.setattr(backend, "create_temp_service", altered)
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert not transport.calls


def test_postgres_concurrent_draws_freeze_single_pool_and_single_reservation(session, settings, monkeypatch):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL row-lock regression runs in CI")
    from pv_growth.database.base import session_scope

    p, s, flags, campaign, users = setup(session, settings, monkeypatch, 3)
    for user in users:
        enter(p, session, s, flags, user)
    session.commit()
    barrier = Barrier(2)

    def worker():
        with session_scope(s) as fresh:
            barrier.wait(timeout=10)
            return p.freeze_draw(
                fresh,
                s,
                FlagService(s),
                campaign_code="pv_daily_lottery",
                day=DAY,
                now=CUTOFF,
            ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        identities = [future.result(timeout=30) for future in futures]
    assert identities[0] == identities[1]
    assert session.query(LotteryDraw).count() == 1
    assert session.query(ExclusiveClaim).count() == 3
    assert session.query(FreeAllocation).count() == 3


def test_stale_draw_never_reserves_yesterdays_budget(session, settings, monkeypatch):
    p, s, flags, campaign, users = setup(session, settings, monkeypatch)
    with pytest.raises(ValidationError):
        p.freeze_draw(session, s, flags, campaign_code=campaign.code, day=DAY - timedelta(days=1), now=CUTOFF)
    assert session.query(FreeAllocation).count() == 0


@pytest.mark.parametrize("rollover_during_gate", [False, True])
def test_stale_pending_delivery_never_creates_a_client(session, settings, monkeypatch, rollover_during_gate):
    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)
    tomorrow = CUTOFF + timedelta(days=1)
    if rollover_during_gate:

        def gate(*args):
            monkeypatch.setattr(p, "utcnow", lambda: tomorrow)
            return AudienceDecision(True, "never_purchased")

        monkeypatch.setattr(p, "decision_for_user", gate)
    else:
        monkeypatch.setattr(p, "utcnow", lambda: tomorrow)
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert not backend.calls and not transport.calls
    assert row.status == "stale" and row.traffic_bytes > 0


def test_lottery_rechecks_panel_after_probe(session, settings, monkeypatch):
    p, s, flags, row, backend, tg, transport = delivery_setup(session, settings, monkeypatch)

    def probe(*args):
        original = backend.service_state
        backend.service_state = lambda ref: {**original(ref), "traffic_used_bytes": row.traffic_bytes}
        return "vless://uuid@8.8.8.8:443", 20

    monkeypatch.setattr(p, "verified_uri", probe)
    assert not p.deliver_winner(session, s, flags, tg, backend, allocation_id=row.id)
    assert not transport.calls
