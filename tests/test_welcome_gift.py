import importlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, Campaign, Event, ExclusiveClaim, FreeAllocation, User
from pv_growth.free_config import budgets, lottery
from pv_growth.free_config.audience import AudienceDecision
from pv_growth.free_config.budgets import CAPS, GIB, MIB, reserve
from pv_growth.provisioning.xui import client_email_for
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

NOW = datetime(2026, 10, 6, 18)
DAY = date(2026, 10, 6)


def test_budget_bigint_aggregate_returns_exact_integer_for_postgres(session, monkeypatch):
    monkeypatch.setattr(session, "scalar", lambda *args: Decimal(100 * MIB))
    used = budgets.used_bytes(session, DAY, "lottery")
    assert type(used) is int and used == 100 * MIB


def module():
    assert importlib.util.find_spec("pv_growth.free_config.welcome_gift"), "welcome gift missing"
    return importlib.import_module("pv_growth.free_config.welcome_gift")


class OriginalDB:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self

    def execute(self, query, params):
        self.queries.append((query, params))

    def fetchone(self):
        return self.row


class Backend:
    def __init__(self):
        self.calls = []
        self.service = None
        self.states = 0

    def health(self):
        return True

    def create_temp_service(self, **kwargs):
        self.calls.append(kwargs)
        self.service = {
            "service_ref": client_email_for(kwargs["idempotency_key"]),
            "traffic_bytes": kwargs["traffic_bytes"],
            "expiry_ts_ms": int((NOW + timedelta(hours=24)).replace(tzinfo=UTC).timestamp() * 1000),
            "client_id": "test-uuid",
            "sub_id": "test-sub",
            "config_uri": "https://sub.example/link/test-sub",
        }
        return self.service

    def service_state(self, ref):
        self.states += 1
        return {
            "exists": True,
            "enabled": True,
            "traffic_limit_bytes": self.service["traffic_bytes"],
            "traffic_used_bytes": 0,
            "expiry_ts_ms": self.service["expiry_ts_ms"],
            "client_id": self.service["client_id"],
            "sub_id": self.service["sub_id"],
        }


def setup(session, settings, monkeypatch):
    p = module()
    s = settings.model_copy(
        update={
            "flag_free_config_enabled": True,
            "flag_pv_exclusive_config_enabled": True,
            "free_channel_id": "@pvnetwork_freeconfig",
        }
    )
    policy = {
        "gift_enabled": True,
        "gift_daily_bytes": 1000 * GIB,
        "gift_day": DAY.isoformat(),
        "gift_traffic_bytes": 100 * MIB,
        "gift_validity_hours": 24,
    }
    session.add(AppConfig(key="pv_free_growth_policy", value=policy))
    session.add(
        Campaign(
            code="pv_welcome_100",
            name="Welcome",
            kind="free_config_exclusive",
            status="active",
            config={"gift": True},
            start_at=datetime(2026, 10, 5, 20, 30),
            end_at=datetime(2026, 10, 6, 20, 30),
        )
    )
    user = User(telegram_user_id=1000, telegram_chat_id=1000)
    session.add(user)
    session.commit()
    original = OriginalDB({"id": "1000", "User_Status": "Active", "agent": "f", "step": "home"})
    monkeypatch.setattr(p.MirzaMySQLReader, "connect", lambda self: original)
    monkeypatch.setattr(p, "decision_for_user", lambda *args: AudienceDecision(True, "never_purchased"))
    monkeypatch.setattr(p, "utcnow", lambda: NOW)
    monkeypatch.setattr(budgets, "utcnow", lambda: NOW, raising=False)
    monkeypatch.setattr(lottery, "utcnow", lambda: NOW)
    monkeypatch.setattr(p, "verified_uri", lambda *args: ("vless://test-uuid@8.8.8.8:443?x=<private>", 20))
    transport = FakeTelegramTransport()
    transport.canned["getChatMember"] = {"status": "member"}
    return p, s, FlagService(s), user, Backend(), TelegramClient(transport, s), transport, original


def request(p, session, s, flags, user, backend, tg):
    return p.request_gift(session, s, flags, tg, user_id=user.id, adapter=backend)


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"gift_enabled": True},
        {"gift_enabled": True, "gift_daily_bytes": True},
        {"gift_enabled": True, "gift_daily_bytes": 0},
    ],
)
def test_gift_budget_requires_explicit_valid_policy(session, monkeypatch, policy):
    monkeypatch.setattr(budgets, "utcnow", lambda: NOW, raising=False)
    session.add(AppConfig(key="pv_free_growth_policy", value=policy))
    session.commit()
    with pytest.raises(ValidationError):
        reserve(session, key="gift:test", day=DAY, bucket="gift", traffic_bytes=100 * MIB)


def test_separate_gift_budget_exact_accounting_all_statuses_and_today_only(session, settings, monkeypatch):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    policy = session.get(AppConfig, "pv_free_growth_policy")
    policy.value = {**policy.value, "gift_daily_bytes": 200 * MIB}
    session.commit()
    first = reserve(session, key="gift:one", day=DAY, bucket="gift", traffic_bytes=100 * MIB)
    first.status = "delivery_unknown"
    reserve(session, key="gift:two", day=DAY, bucket="gift", traffic_bytes=100 * MIB)
    with pytest.raises(ValidationError):
        reserve(session, key="gift:three", day=DAY, bucket="gift", traffic_bytes=100 * MIB)
    with pytest.raises(ValidationError):
        reserve(
            session, key="gift:tomorrow", day=DAY + timedelta(days=1), bucket="gift", traffic_bytes=100 * MIB
        )
    assert CAPS == {"public": 10 * GIB, "lottery": 50 * GIB}
    assert session.query(FreeAllocation).count() == 2


@pytest.mark.parametrize("change", ["oversized", "redated"])
def test_policy_cannot_expand_the_authorized_one_day_gift(session, settings, monkeypatch, change):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    policy = session.get(AppConfig, "pv_free_growth_policy")
    day = DAY
    if change == "oversized":
        policy.value = {**policy.value, "gift_daily_bytes": 1001 * GIB}
    else:
        day += timedelta(days=1)
        policy.value = {**policy.value, "gift_day": day.isoformat()}
        monkeypatch.setattr(budgets, "utcnow", lambda: NOW + timedelta(days=1))
    session.commit()
    with pytest.raises(ValidationError):
        reserve(session, key="gift:expanded", day=day, bucket="gift", traffic_bytes=100 * MIB)


@pytest.mark.parametrize("stage", ["before_create", "before_send"])
def test_gift_campaign_end_during_final_gate_stops_external_effect(session, settings, monkeypatch, stage):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    campaign = session.query(Campaign).one()
    campaign.end_at = NOW + timedelta(minutes=1)
    session.commit()
    gate = p._require_audience
    rounds = 0

    def checked(*args):
        nonlocal rounds
        result = gate(*args)
        rounds += 1
        if rounds == (3 if stage == "before_create" else 5):
            monkeypatch.setattr(p, "utcnow", lambda: NOW + timedelta(minutes=2))
        return result

    monkeypatch.setattr(p, "_require_audience", checked)
    assert request(p, session, s, flags, user, backend, tg) == "unavailable"
    assert not transport.sent_texts()
    if stage == "before_create":
        assert not backend.calls


def test_gift_is_exact_100mib_owned_tunnel_verified_and_single_delivery(session, settings, monkeypatch):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    assert request(p, session, s, flags, user, backend, tg) == "delivered"
    assert request(p, session, s, flags, user, backend, tg) == "already_reserved"
    row = session.query(FreeAllocation).one()
    claim = session.query(ExclusiveClaim).one()
    assert row.bucket == "gift" and row.traffic_bytes == 100 * MIB and row.status == "delivered"
    assert (
        claim.traffic_bytes == 100 * MIB
        and claim.traffic_gb == 0
        and claim.validity_hours == 24
        and claim.status == "active"
    )
    assert backend.calls[0]["traffic_bytes"] == 100 * MIB and backend.calls[0]["validity_hours"] == 24
    assert backend.states >= 2
    assert len(transport.sent_texts()) == 1 and "&lt;private&gt;" in transport.sent_texts()[0]
    assert (
        row.payload["tunnel_plan"]["panel_id"] == 33 and len(row.payload["tunnel_plan"]["inbound_ids"]) == 9
    )
    assert (
        row.payload["message_id"]
        and session.query(Event).filter(Event.event_type == "TRIAL_CREATED").count() == 1
    )
    assert original.queries and all(
        q.startswith("SELECT id,User_Status,agent,step FROM user WHERE id=%s") for q, _ in original.queries
    )


@pytest.mark.parametrize(
    "reason",
    ["dormant", "recent_purchase", "policy_disabled", "history_unavailable", "active_or_unverified_service"],
)
def test_gift_rejects_any_user_not_proven_never_purchased(session, settings, monkeypatch, reason):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    monkeypatch.setattr(
        p,
        "decision_for_user",
        lambda *args: AudienceDecision(reason in {"dormant", "policy_disabled"}, reason),
    )
    with pytest.raises(ValidationError):
        request(p, session, s, flags, user, backend, tg)
    assert not backend.calls and not transport.sent_texts() and session.query(FreeAllocation).count() == 0


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"id": "1001", "User_Status": "Active", "agent": "f"},
        {"id": "1000", "User_Status": "blocked", "agent": "f"},
        {"id": "1000", "User_Status": "Active", "agent": "admin"},
    ],
)
def test_original_mainbot_user_must_exist_active_regular(session, settings, monkeypatch, row):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    original.row = row
    with pytest.raises(ValidationError):
        request(p, session, s, flags, user, backend, tg)
    assert not backend.calls and not transport.sent_texts() and session.query(FreeAllocation).count() == 0


@pytest.mark.parametrize("gate", ["membership", "group", "disabled", "budget", "tomorrow"])
def test_gift_missing_gate_never_grants_or_promises_delivery(session, settings, monkeypatch, gate):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    if gate == "membership":
        transport.canned["getChatMember"] = {"status": "left"}
    elif gate == "group":
        user.telegram_chat_id = -100123
        session.commit()
    elif gate in {"disabled", "budget"}:
        row = session.get(AppConfig, "pv_free_growth_policy")
        row.value = {
            **row.value,
            **({"gift_enabled": False} if gate == "disabled" else {"gift_daily_bytes": 50 * MIB}),
        }
        session.commit()
    else:
        monkeypatch.setattr(p, "utcnow", lambda: NOW + timedelta(days=1))
        monkeypatch.setattr(budgets, "utcnow", lambda: NOW + timedelta(days=1))
    try:
        result = request(p, session, s, flags, user, backend, tg)
        assert result == "unavailable"
    except ValidationError:
        pass
    assert not backend.calls and not transport.sent_texts() and session.query(FreeAllocation).count() == 0


@pytest.mark.parametrize(
    "stage",
    [
        "health",
        "unknown",
        "identity",
        "spent_after_health",
        "buyer_after_health",
        "midnight_after_eligibility",
    ],
)
def test_gift_failures_keep_reservation_and_do_not_leak_second_service_or_send(
    session, settings, monkeypatch, stage
):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    if stage == "health":
        monkeypatch.setattr(
            p, "verified_uri", lambda *args: (_ for _ in ()).throw(ValidationError("health failed"))
        )
    elif stage == "unknown":
        transport.fail_methods.add("sendMessage")
    elif stage == "identity":
        state = backend.service_state
        monkeypatch.setattr(backend, "service_state", lambda ref: {**state(ref), "client_id": "other-client"})
    elif stage == "spent_after_health":
        state = backend.service_state
        monkeypatch.setattr(
            backend,
            "service_state",
            lambda ref: {**state(ref), "traffic_used_bytes": 100 * MIB if backend.states > 1 else 0},
        )
    elif stage == "buyer_after_health":

        def probe(*args):
            monkeypatch.setattr(
                p, "decision_for_user", lambda *args: AudienceDecision(False, "recent_purchase")
            )
            return "vless://test-uuid@8.8.8.8:443", 20

        monkeypatch.setattr(p, "verified_uri", probe)
    else:

        def history(*args):
            monkeypatch.setattr(p, "utcnow", lambda: NOW + timedelta(days=1))
            monkeypatch.setattr(budgets, "utcnow", lambda: NOW + timedelta(days=1))
            return AudienceDecision(True, "never_purchased")

        monkeypatch.setattr(p, "decision_for_user", history)
    assert request(p, session, s, flags, user, backend, tg) == "unavailable"
    assert not transport.sent_texts() or stage == "unknown"
    if stage == "midnight_after_eligibility":
        assert not backend.calls and session.query(FreeAllocation).count() == 0
    else:
        assert session.query(FreeAllocation).one().traffic_bytes == 100 * MIB
        assert session.query(ExclusiveClaim).one().status != "active"
        if stage == "unknown":
            assert request(p, session, s, flags, user, backend, tg) == "already_reserved"
            assert len(backend.calls) == 1 and len(transport.sent_texts()) == 1


def test_gift_dialogue_gate_is_readonly_and_requires_todays_policy(session, settings, monkeypatch):
    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    assert callable(getattr(p, "gift_enabled", None)), "public gift dialogue gate missing"
    assert p.gift_enabled(session, s, flags)
    assert not original.queries and not transport.calls and not backend.calls
    row = session.get(AppConfig, "pv_free_growth_policy")
    row.value = {**row.value, "gift_enabled": False}
    session.commit()
    assert not p.gift_enabled(session, s, flags)


def test_postgres_concurrent_callbacks_reserve_and_send_one_gift(session, settings, monkeypatch):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL gift locking regression runs in CI")
    from pv_growth.database.base import session_scope

    p, s, flags, user, backend, tg, transport, original = setup(session, settings, monkeypatch)
    user_id = user.id
    barrier = Barrier(2)

    def worker():
        with session_scope(s) as fresh:
            barrier.wait(timeout=10)
            return p.request_gift(fresh, s, FlagService(s), tg, user_id=user_id, adapter=backend)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == ["already_reserved", "delivered"]
    assert session.query(FreeAllocation).count() == 1
    assert session.query(ExclusiveClaim).count() == 1
    assert len(backend.calls) == 1 and len(transport.sent_texts()) == 1
