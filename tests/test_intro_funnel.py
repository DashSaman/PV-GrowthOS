import html
import re
from datetime import UTC, datetime

import pytest

from pv_growth.database.models import AppConfig, Campaign, FreeAllocation, Job, User
from pv_growth.free_config.budgets import GIB
from pv_growth.provisioning.xui import client_email_for


class Reader:
    def __init__(self):
        self.history = ([], [], [])

    def fetch_private_invite_history(self, recipient, *, require_home=True):
        return self.history


class Bot:
    def __init__(self):
        self.calls = []
        self.fail = False

    def call(self, method, payload, *, guard=None):
        if guard:
            guard()
        self.calls.append((method, payload))
        if method == "getMe":
            return {"username": "pvnetwork_bot", "is_bot": True, "id": 123}
        if method == "getChat":
            return {"id": payload["chat_id"], "type": "private"}
        if self.fail:
            raise RuntimeError("ambiguous response")
        return {
            "chat": {"id": payload["chat_id"], "type": "private"},
            "message_id": 99,
            "date": int(datetime(2026, 10, 13, 4, tzinfo=UTC).timestamp()),
            "text": html.unescape(re.sub(r"<[^>]+>", "", payload["text"])),
            "reply_markup": payload["reply_markup"],
        }


class Adapter:
    def __init__(self):
        self.created = []

    def health(self):
        return True

    def create_temp_service(self, **kwargs):
        self.created.append(kwargs)
        return {
            "service_ref": client_email_for(kwargs["idempotency_key"]),
            "client_id": "uuid",
            "sub_id": "sub",
            "traffic_bytes": kwargs["traffic_bytes"],
            "expiry_ts_ms": int(datetime(2026, 10, 14, 4, tzinfo=UTC).timestamp() * 1000),
            "config_uri": "vless://uuid@8.8.8.8:443",
        }

    def service_state(self, ref):
        return {
            "exists": True,
            "enabled": True,
            "client_id": "uuid",
            "sub_id": "sub",
            "traffic_limit_bytes": 5 * GIB,
            "traffic_used_bytes": 0,
            "expiry_ts_ms": int(datetime(2026, 10, 14, 4, tzinfo=UTC).timestamp() * 1000),
        }


def ready(session, settings, monkeypatch):
    from pv_growth.acquisition import intro as p

    now = datetime(2026, 10, 13, 4)
    monkeypatch.setattr(p, "utcnow", lambda: now)
    monkeypatch.setattr("pv_growth.free_config.lottery.utcnow", lambda: now)
    monkeypatch.setattr(p, "verified_uri", lambda *a: ("vless://uuid@8.8.8.8:443#PV", 10))
    s = settings.model_copy(
        update={
            "flag_free_config_enabled": True,
            "flag_pv_exclusive_config_enabled": True,
            "purchase_bot_token": "123:test",
        }
    )
    session.add(
        AppConfig(
            key="pv_intro_policy",
            value={
                "enabled": True,
                "campaign_code": "pv_intro_5g_20261013",
                "total_bytes": 10000 * GIB,
                "traffic_bytes": 5 * GIB,
                "validity_hours": 24,
                "dormant_days": 30,
                "followup_hours": 24,
                "channel_followup_hours": 48,
                "planning_day": "2026-10-13",
                "batch_cap": 5,
            },
        )
    )
    campaign = Campaign(
        code="pv_intro_5g_20261013",
        name="Intro",
        kind="acquisition_intro",
        status="active",
        config={"intro_funnel": True},
    )
    user = User(telegram_user_id=12345, telegram_chat_id=12345)
    session.add_all([campaign, user])
    session.commit()
    return p, s, user, Reader(), Bot(), Adapter()


def test_gift_is_exact_main_five_gib_and_followup_starts_at_confirmed_delivery(
    session, settings, monkeypatch
):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert row.traffic_bytes == 5 * GIB and row.bucket == "acq_intro"
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert len(adapter.created) == 1 and adapter.created[0]["traffic_bytes"] == 5 * GIB
    assert adapter.created[0]["validity_hours"] == 24
    jobs = session.query(Job).filter_by(job_type="acquisition.intro_followup").all()
    assert sorted(j.scheduled_at for j in jobs) == [datetime(2026, 10, 14, 4), datetime(2026, 10, 15, 4)]
    assert row.payload["tunnel_plan"]
    assert p.reserve_intro(session, s, user_id=user.id, reader=reader) is None
    assert not p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert len(adapter.created) == 1


def test_concurrent_last_five_gib_reserves_once_in_postgres(session, settings, monkeypatch):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL total-budget locking exercised in CI")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from pv_growth.database.base import session_scope

    p, s, first, reader, bot, adapter = ready(session, settings, monkeypatch)
    second = User(telegram_user_id=65432, telegram_chat_id=65432)
    session.add(second)
    campaign = session.query(Campaign).one()
    session.add(
        FreeAllocation(
            id="consumed-budget",
            day=datetime(2026, 10, 12).date(),
            bucket="acq_intro",
            campaign_id=campaign.id,
            traffic_bytes=9995 * GIB,
            status="delivery_unknown",
        )
    )
    session.commit()
    ids = [first.id, second.id]
    barrier = Barrier(2)

    def attempt(uid):
        with session_scope(s) as db:
            barrier.wait(timeout=10)
            return p.reserve_intro(db, s, user_id=uid, reader=Reader()) is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ids))
    assert sum(results) == 1
    session.expire_all()
    assert sum(r.traffic_bytes for r in session.query(FreeAllocation).all()) == 10000 * GIB


def test_purchase_during_probe_prevents_send_and_followup(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)

    def probe(*args):
        reader.history = ([], [], [{"payment_Status": "paid", "price": 100, "time": "2026-10-13 07:30:00"}])
        return "vless://uuid@8.8.8.8:443", 10

    monkeypatch.setattr(p, "verified_uri", probe)
    assert not p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert not [v for m, v in bot.calls if m == "sendMessage"]
    assert not session.query(Job).filter_by(job_type="acquisition.intro_followup").count()


def test_old_welcome_reservation_is_not_another_intro(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    session.add(
        FreeAllocation(
            id="old-welcome",
            day=datetime(2026, 10, 6).date(),
            bucket="gift",
            user_id=user.id,
            traffic_bytes=100 * 1024**2,
            status="delivery_unknown",
        )
    )
    session.commit()
    assert p.reserve_intro(session, s, user_id=user.id, reader=reader) is None


def test_planner_is_bounded_and_does_not_serialize_unbounded_contacts(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)

    class CountingReader(Reader):
        calls = 0

        def fetch_private_invite_history(self, recipient, *, require_home=True):
            self.calls += 1
            return None

    bounded = CountingReader()
    monkeypatch.setattr(p, "MirzaMySQLReader", lambda s: bounded)
    session.add(
        AppConfig(
            key="pv_intro_cohort_20261013",
            value={"contacts": list(range(100, 300)), "cursor": 0, "status": "running"},
        )
    )
    session.commit()
    p.plan_job(session, s, {})
    assert bounded.calls == 30
    assert session.get(AppConfig, "pv_intro_cohort_20261013").value["cursor"] == 30
    assert not session.query(FreeAllocation).count()


def test_actual_post_purchase_history_is_distinguished_from_unknown_history(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    reader.history = None
    assert not p.followup(session, s, row.id, reader=reader, bot=bot)
    assert row.payload["followup_status"] == "history_blocked"


def test_panel_mismatch_never_delivers_or_retries(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    original = adapter.service_state
    adapter.service_state = lambda ref: {**original(ref), "traffic_limit_bytes": 100 * GIB}
    assert not p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert not p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert row.status == "provision_unknown"
    assert len(adapter.created) == 1
    assert not [v for m, v in bot.calls if m == "sendMessage"]


def test_scheduler_and_registry_plan_only_authorized_frozen_cohort(session, settings, monkeypatch):
    from pv_growth.jobs.runner import _HANDLERS, register_builtin_handlers
    from pv_growth.jobs.scheduler import enqueue_free_growth

    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    session.add(
        AppConfig(
            key="pv_intro_cohort_20261013", value={"contacts": [12345], "cursor": 0, "status": "running"}
        )
    )
    session.commit()
    enqueue_free_growth(session, s, now=datetime(2026, 10, 13, 4, 1))
    enqueue_free_growth(session, s, now=datetime(2026, 10, 13, 4, 2))
    assert session.query(Job).filter_by(job_type="acquisition.intro_plan").count() == 2
    assert session.query(Job).filter_by(job_type="acquisition.intro_plan").first().max_attempts == 1
    register_builtin_handlers()
    assert all(
        k in _HANDLERS
        for k in ("acquisition.intro_plan", "acquisition.intro_deliver", "acquisition.intro_followup")
    )


def test_thirty_day_dormant_and_never_buyer_are_eligible_but_recent_active_unknown_not(
    session, settings, monkeypatch
):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    reader.history = (
        [{"Status": "disabledn", "price_product": 100, "time_sell": "2026-09-10 07:30:00"}],
        [],
        [],
    )
    assert p.reserve_intro(session, s, user_id=user.id, reader=reader)
    other = User(telegram_user_id=9876, telegram_chat_id=9876)
    session.add(other)
    session.commit()
    reader.history = ([{"Status": "active"}], [], [])
    assert p.reserve_intro(session, s, user_id=other.id, reader=reader) is None
    reader.history = ([], [], [{"payment_Status": "paid", "price": 100, "time": "2026-10-12 07:30:00"}])
    assert p.reserve_intro(session, s, user_id=other.id, reader=reader) is None
    reader.history = None
    assert p.reserve_intro(session, s, user_id=other.id, reader=reader) is None


def test_campaign_budget_is_total_across_days_and_unknowns_count(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    campaign = session.query(Campaign).one()
    session.add(
        FreeAllocation(
            id="previous-day",
            day=datetime(2026, 10, 12).date(),
            bucket="acq_intro",
            campaign_id=campaign.id,
            traffic_bytes=9996 * GIB,
            status="delivery_unknown",
        )
    )
    session.commit()
    assert p.reserve_intro(session, s, user_id=user.id, reader=reader) is None
    assert session.query(FreeAllocation).count() == 1


def test_ambiguous_provision_or_send_never_recreates_and_never_schedules_followup(
    session, settings, monkeypatch
):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    bot.fail = True
    assert not p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert not p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert len(adapter.created) == 1 and row.status == "delivery_unknown"
    assert not session.query(Job).filter_by(job_type="acquisition.intro_followup").count()


def test_followup_once_after_twenty_four_hours_and_cancel_after_purchase(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    assert not p.followup(session, s, row.id, reader=reader, bot=bot)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    reader.history = ([], [], [{"payment_Status": "paid", "price": 100, "time": "2026-10-13 15:30:00"}])
    assert not p.followup(session, s, row.id, reader=reader, bot=bot)
    assert row.payload["followup_status"] == "stopped_after_purchase"
    assert len([x for x in bot.calls if x[0] == "sendMessage"]) == 1


def test_followup_sends_one_clean_channel_offer_without_another_grant(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    assert p.followup(session, s, row.id, reader=reader, bot=bot)
    assert not p.followup(session, s, row.id, reader=reader, bot=bot)
    sends = [v for m, v in bot.calls if m == "sendMessage"]
    assert len(sends) == 2 and "pvnetwork_freeconfig" not in sends[-1]["text"]
    assert "۲۴ ساعت" in sends[-1]["text"]
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 15, 4))
    assert p.followup(session, s, row.id, stage=48, reader=reader, bot=bot)
    assert not p.followup(session, s, row.id, stage=48, reader=reader, bot=bot)
    sends = [v for m, v in bot.calls if m == "sendMessage"]
    assert len(sends) == 3 and "pvnetwork_freeconfig" in sends[-1]["text"]
    assert len(adapter.created) == 1


def test_today_only_reservation_and_early_channel_block(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    assert not p.followup(session, s, row.id, stage=48, reader=reader, bot=bot)
    other = User(telegram_user_id=54321, telegram_chat_id=54321)
    session.add(other)
    session.commit()

    assert p.reserve_intro(session, s, user_id=other.id, reader=reader) is None
    assert p.followup(session, s, row.id, reader=reader, bot=bot)


def test_purchase_after_buy_offer_stops_channel_offer(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    assert p.followup(session, s, row.id, reader=reader, bot=bot)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 15, 4))
    reader.history = ([], [], [{"payment_Status": "paid", "price": 100, "time": "2026-10-14 15:30:00"}])
    assert not p.followup(session, s, row.id, stage=48, reader=reader, bot=bot)
    assert row.payload["channel_followup_status"] == "stopped_after_purchase"
    assert len([v for m, v in bot.calls if m == "sendMessage"]) == 2


def test_unknown_purchase_offer_prevents_channel_followup(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    bot.fail = True
    assert not p.followup(session, s, row.id, reader=reader, bot=bot)
    bot.fail = False
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 15, 4))
    assert not p.followup(session, s, row.id, stage=48, reader=reader, bot=bot)
    assert row.payload["channel_followup_status"] == "purchase_stage_incomplete"


def test_delivery_backpressure_preserves_cohort_cursor(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    session.add(
        AppConfig(
            key="pv_intro_cohort_20261013", value={"contacts": [12345], "cursor": 0, "status": "running"}
        )
    )
    for i in range(20):
        session.add(
            Job(
                job_type="acquisition.intro_deliver",
                payload={},
                idempotency_key=f"pending-{i}",
                max_attempts=1,
            )
        )
    session.commit()
    p.plan_job(session, s, {})
    assert session.get(AppConfig, "pv_intro_cohort_20261013").value["cursor"] == 0
    assert not session.query(FreeAllocation).count()


def test_legacy_followup_job_maps_to_purchase_and_migration_is_idempotent(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    row = p.reserve_intro(session, s, user_id=user.id, reader=reader)
    assert p.deliver_intro(session, s, row.id, reader=reader, bot=bot, adapter=adapter)
    original = session.query(Job).filter_by(idempotency_key=f"intro-followup:{row.id}").one()
    original.payload = {"allocation_id": row.id}
    session.commit()
    p.schedule_followups(session, row)
    p.schedule_followups(session, row)
    session.commit()
    assert session.query(Job).filter_by(job_type="acquisition.intro_followup").count() == 2
    calls = []
    monkeypatch.setattr(p, "followup", lambda *a, **kw: calls.append(kw["stage"]))
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    p.followup_job(session, s, original.payload)
    assert calls == [24]


def test_duplicate_planner_in_one_minute_cannot_exceed_five(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    monkeypatch.setattr(p, "MirzaMySQLReader", lambda s: reader)
    session.add(
        AppConfig(
            key="pv_intro_cohort_20261013",
            value={"contacts": list(range(100, 120)), "cursor": 0, "status": "running"},
        )
    )
    session.commit()
    p.plan_job(session, s, {})
    p.plan_job(session, s, {})
    assert session.query(FreeAllocation).count() == 5
    assert session.get(AppConfig, "pv_intro_cohort_20261013").value["cursor"] == 5


def test_intro_checks_all_existing_user_steps_without_relaxing_history(session, settings, monkeypatch):
    p, s, user, reader, bot, adapter = ready(session, settings, monkeypatch)
    calls = []

    class AnyStepReader(Reader):
        def fetch_private_invite_history(self, recipient, *, require_home=True):
            calls.append(require_home)
            return None if require_home else ([], [], [])

    all_steps = AnyStepReader()
    row = p.reserve_intro(session, s, user_id=user.id, reader=all_steps)
    assert row and calls == [False]
    assert p.deliver_intro(session, s, row.id, reader=all_steps, bot=bot, adapter=adapter)
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 14, 4))
    assert p.followup(session, s, row.id, reader=all_steps, bot=bot)
    assert calls and not any(calls)
