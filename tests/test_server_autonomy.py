import importlib.util
from datetime import date, datetime

from pv_growth.database.models import AppConfig, FreeAllocation, Job, PublishedPost
from pv_growth.free_config import shared_public
from pv_growth.provisioning.xui import ProvisioningError
from tests.test_shared_public import Backend, setup


def enable(session, **values):
    row = session.get(AppConfig, "pv_free_growth_policy")
    row.value = {**row.value, "server_autonomy_enabled": True, "owned_shared_active_limit": 2, **values}
    session.commit()


def test_two_active_owned_clients_block_third_without_spending_budget(session, settings, monkeypatch):
    s, flags, tg, transport = setup(session, settings)
    enable(session)
    backend = Backend()
    monkeypatch.setattr(shared_public, "local_day", lambda now=None: date(2026, 10, 13))
    monkeypatch.setattr(shared_public, "verified_uri", lambda *a: ("vless://test@8.8.8.8:443", 10))
    for slot in (1, 2):
        assert shared_public.publish_shared_slot(
            session, s, flags, tg, backend, day=date(2026, 10, 13), slot=slot
        )
    assert (
        shared_public.publish_shared_slot(session, s, flags, tg, backend, day=date(2026, 10, 13), slot=3)
        is None
    )
    assert len(backend.calls) == 2
    assert session.query(FreeAllocation).count() == 2


def test_replacement_requires_authoritative_end_and_waits_on_unknown_state(session, settings, monkeypatch):
    s, flags, tg, transport = setup(session, settings)
    enable(session)
    backend = Backend()
    monkeypatch.setattr(shared_public, "local_day", lambda now=None: date(2026, 10, 13))
    monkeypatch.setattr(shared_public, "verified_uri", lambda *a: ("vless://test@8.8.8.8:443", 10))
    for slot in (1, 2):
        shared_public.publish_shared_slot(session, s, flags, tg, backend, day=date(2026, 10, 13), slot=slot)
    first = session.get(FreeAllocation, "pv_shared:2026-10-13:1")
    original = backend.service_state

    def unavailable(ref):
        if ref == first.service_ref:
            raise ProvisioningError("panel client lookup unavailable")
        return original(ref)

    backend.service_state = unavailable
    assert (
        shared_public.publish_shared_slot(session, s, flags, tg, backend, day=date(2026, 10, 13), slot=3)
        is None
    )
    backend.service_state = lambda ref: {**original(ref), "quota_exhausted": ref == first.service_ref}
    assert shared_public.publish_shared_slot(session, s, flags, tg, backend, day=date(2026, 10, 13), slot=3)
    assert len(backend.calls) == 3


def test_ambiguous_owned_creation_is_never_retried(session, settings, monkeypatch):
    s, flags, tg, transport = setup(session, settings)
    enable(session)
    backend = Backend()
    monkeypatch.setattr(shared_public, "local_day", lambda now=None: date(2026, 10, 13))
    calls = []

    def ambiguous(**kwargs):
        calls.append(kwargs)
        raise ProvisioningError("xui transport: ReadTimeout")

    backend.create_temp_service = ambiguous
    for _ in range(2):
        shared_public.publish_shared_slot(session, s, flags, tg, backend, day=date(2026, 10, 13), slot=1)
    assert len(calls) == 1
    assert session.get(FreeAllocation, "pv_shared:2026-10-13:1").status == "provision_unknown"
    assert not transport.sent_texts()


def autonomous():
    assert importlib.util.find_spec("pv_growth.free_config.autonomous"), "server-owned fulfillment is missing"
    from pv_growth.free_config import autonomous as module

    return module


def test_unfilled_web_recovers_without_resetting_failed_job_or_replaying_receipt(
    session, settings, monkeypatch
):
    from tests.test_direct_web_publication import ready_direct

    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    enable(session, community_health_attempts_per_day=120)
    p = autonomous()
    stamp = datetime(2026, 10, 12, 8)
    monkeypatch.setattr(p, "utcnow", lambda: stamp)
    from pv_growth.free_config import pipeline

    monkeypatch.setattr(pipeline, "utcnow", lambda: stamp)
    monkeypatch.setattr("pv_growth.free_config.shared_public.utcnow", lambda: stamp)
    old = Job(
        job_type="free_config.publish",
        status="failed",
        attempt=5,
        max_attempts=5,
        idempotency_key="free_direct:2026-10-12:3",
        payload={"day": "2026-10-12", "slot": 3},
    )
    session.add(old)
    session.commit()
    monkeypatch.setattr(pipeline, "rank_candidates", lambda *a, **kw: [])
    assert p.fulfill_web(session, s, tg) is None
    assert session.query(PublishedPost).count() == 0
    monkeypatch.setattr(pipeline, "rank_candidates", lambda *a, **kw: [row])
    assert p.fulfill_web(session, s, tg) is not None
    assert old.status == "failed" and old.attempt == 5
    # The same config cannot be sent again even though other slots are due.
    assert p.fulfill_web(session, s, tg) is None
    assert len(transport.sent_texts()) == 1


def test_unknown_legacy_reservations_and_midnight_consume_web_cap(session, settings, monkeypatch):
    from tests.test_direct_web_publication import ready_direct

    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    enable(session)
    p = autonomous()
    for i in range(10):
        session.add(
            PublishedPost(
                dedupe_key=f"unknown:{i}",
                channel_id="pvnetwork_freeconfig",
                kind="free_public",
                posted_at=datetime(2026, 10, 11, 21),
            )
        )
    session.commit()
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 12, 14))
    assert p.fulfill_web(session, s, tg) is None
    assert not transport.sent_texts()
    assert p.due_web_target(session, datetime(2026, 10, 12, 20, 45)) == 0
    assert p.due_web_target(session, datetime(2026, 10, 12, 22, 5)) == 1


def test_scheduler_installs_fulfillment_and_native_daily_report_without_codex(session, settings, monkeypatch):
    from pv_growth.jobs.scheduler import enqueue_free_growth

    session.add(
        AppConfig(
            key="pv_free_growth_policy",
            value={
                "server_autonomy_enabled": True,
                "community_enabled": True,
                "community_direct_enabled": True,
                "community_daily_slots": 10,
            },
        )
    )
    session.commit()
    now = datetime(2026, 10, 13, 14, 25)
    enqueue_free_growth(session, settings, now)
    enqueue_free_growth(session, settings, now)
    assert session.query(Job).filter_by(job_type="free_config.fulfill_web").count() == 1
    enqueue_free_growth(session, settings, datetime(2026, 10, 13, 20, 25))
    assert session.query(Job).filter_by(job_type="runtime.daily_report").count() == 1


class ManagerClient:
    def __init__(self, owner=True):
        self.owner, self.sent = owner, []

    def get_chat_member(self, channel, user):
        return {"status": "creator" if self.owner else "administrator", "user": {"id": user}}

    def send_message(self, chat, body, keyboard=None):
        self.sent.append(body)
        return {"message_id": len(self.sent), "chat": {"id": chat}}

    def answer_callback_query(self, *a):
        return {}


def manager():
    assert importlib.util.find_spec("pv_growth.telegram.management"), "private owner controls missing"
    from pv_growth.telegram import management

    return management


def test_owner_controls_fail_closed_and_reject_group_impersonation(session, settings):
    m = manager()
    s = settings.model_copy(
        update={"official_channel_id": "@pvnetwork0", "free_channel_id": "@pvnetwork_freeconfig"}
    )
    nonowner = ManagerClient(False)
    assert m.handle_action(
        session, s, nonowner, user_id=42, chat_id=42, chat_type="private", action="shared:off"
    )
    assert session.get(AppConfig, "pv_free_growth_policy") is None
    assert m.handle_action(
        session, s, ManagerClient(), user_id=42, chat_id=-100, chat_type="group", action="shared:off"
    )
    assert session.get(AppConfig, "pv_free_growth_policy") is None


def test_owner_controls_only_bounded_growth_policy_and_schedule_tomorrow(session, settings, monkeypatch):
    m = manager()
    s = settings.model_copy(
        update={"official_channel_id": "@pvnetwork0", "free_channel_id": "@pvnetwork_freeconfig"}
    )
    monkeypatch.setattr(m, "utcnow", lambda: datetime(2026, 10, 13, 14))
    client = ManagerClient()
    session.add(AppConfig(key="pv_free_growth_policy", value={"weekly_member_gift_enabled": True}))
    session.commit()
    for action in ("active:2", "web:10", "shared:on"):
        m.handle_action(session, s, client, user_id=42, chat_id=42, chat_type="private", action=action)
    policy = session.get(AppConfig, "pv_free_growth_policy").value
    assert policy["owned_shared_active_limit"] == 2 and policy["community_daily_slots"] == 10
    assert (
        policy["owned_shared_channel"] == "@pvnetwork_freeconfig" and not policy["owned_main_direct_enabled"]
    )
    for action in ("active:3", "web:11", "gift_budget:1000", "intro:replay", "main:on"):
        before = dict(policy)
        m.handle_action(session, s, client, user_id=42, chat_id=42, chat_type="private", action=action)
        policy = session.get(AppConfig, "pv_free_growth_policy").value
        assert policy == before
    m.handle_action(
        session,
        s,
        client,
        user_id=42,
        chat_id=42,
        chat_type="private",
        action="hours:01:30,07:30,09:30,11:30,13:30,15:30,17:30,19:30,21:30,23:30",
    )
    assert (
        session.get(AppConfig, "pv_free_growth_policy").value["public_schedule_effective_day"] == "2026-10-14"
    )
    assert session.query(Job).count() == 0
    assert session.query(FreeAllocation).count() == 0


def test_native_admin_report_has_one_durable_attempt_and_unknown_is_not_replayed(
    session, settings, monkeypatch
):
    from pv_growth.telegram import client as telegram

    m = manager()
    s = settings.model_copy(
        update={"official_channel_id": "@pvnetwork0", "free_channel_id": "@pvnetwork_freeconfig"}
    )
    monkeypatch.setattr(m, "utcnow", lambda: datetime(2026, 10, 13, 20, 25))
    client = ManagerClient()
    m.handle_action(session, s, client, user_id=42, chat_id=42, chat_type="private", action="menu")
    calls = []

    def ambiguous(*args):
        calls.append(args)
        raise RuntimeError("ambiguous fake send")

    client.send_message = ambiguous
    monkeypatch.setattr(telegram, "get_telegram", lambda cfg: client)
    m.daily_report(session, s, {"day": "2026-10-13"})
    m.daily_report(session, s, {"day": "2026-10-13"})
    from pv_growth.database.models import MessageLog

    assert len(calls) == 1
    receipt = session.query(MessageLog).filter_by(purpose="native_admin_report").one()
    assert receipt.status == "delivery_unknown" and receipt.attempts == 1


def test_manager_commands_are_connected_to_real_private_update_routes(session, settings, monkeypatch):
    from pv_growth.api import webhooks

    s = settings.model_copy(
        update={"official_channel_id": "@pvnetwork0", "free_channel_id": "@pvnetwork_freeconfig"}
    )
    client = ManagerClient()
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    webhooks._handle_message(
        client,
        webhooks.TgMessage.model_validate(
            {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/manage"}
        ),
    )
    webhooks._handle_callback(
        client,
        webhooks.TgCallback.model_validate(
            {
                "id": "fake",
                "from": {"id": 42},
                "data": "manage:active:2",
                "message": {"chat": {"id": 42, "type": "private"}},
            }
        ),
    )
    session.expire_all()
    assert session.get(AppConfig, "pv_free_growth_policy").value["owned_shared_active_limit"] == 2
    assert len(client.sent) == 2


def test_candidate_checks_have_a_durable_daily_limit(session, settings, monkeypatch):
    s, flags, tg, transport = setup(session, settings)
    enable(session, community_health_attempts_per_day=2)
    p = autonomous()
    monkeypatch.setattr(p, "utcnow", lambda: datetime(2026, 10, 13, 4))
    assert p.probe_allowance(session, 5) == 2
    session.commit()
    assert p.probe_allowance(session, 5) == 0
    assert session.get(AppConfig, "web-probe-budget:2026-10-13").value["attempts"] == 2


def test_pending_fulfillment_does_not_accumulate_a_parallel_backlog(session, settings):
    from pv_growth.jobs.scheduler import enqueue_free_growth

    session.add(
        AppConfig(
            key="pv_free_growth_policy",
            value={
                "server_autonomy_enabled": True,
                "community_enabled": True,
                "community_direct_enabled": True,
                "community_daily_slots": 10,
            },
        )
    )
    session.commit()
    for minute in (5, 10, 15, 20):
        enqueue_free_growth(session, settings, datetime(2026, 10, 13, 14, minute))
    assert session.query(Job).filter_by(job_type="free_config.fulfill_web").count() == 1


def test_remote_shared_call_is_owned_before_a_second_worker_can_reenter(session, settings, monkeypatch):
    from pv_growth.database.base import session_scope

    s, flags, tg, transport = setup(session, settings)
    enable(session)
    backend = Backend()
    other = Backend()
    original = backend.create_temp_service
    monkeypatch.setattr(shared_public, "local_day", lambda now=None: date(2026, 10, 13))
    monkeypatch.setattr(shared_public, "verified_uri", lambda *a: ("vless://test@8.8.8.8:443", 10))

    def reenter(**kwargs):
        with session_scope(s) as second:
            assert (
                shared_public.publish_shared_slot(second, s, flags, tg, other, day=date(2026, 10, 13), slot=1)
                is None
            )
        return original(**kwargs)

    backend.create_temp_service = reenter
    assert shared_public.publish_shared_slot(session, s, flags, tg, backend, day=date(2026, 10, 13), slot=1)
    assert len(backend.calls) == 1 and not other.calls


def test_public_uri_aliases_do_not_inflate_daily_distinct_count(session, settings, monkeypatch):
    from pv_growth.database.models import RawConfig
    from pv_growth.free_config import pipeline
    from pv_growth.free_config.web_publication import config_alias_exclusions
    from tests.test_direct_web_publication import ready_direct

    s, row, transport, tg = ready_direct(session, settings, monkeypatch)
    enable(session)
    assert pipeline.publish_public_slot(
        session,
        s,
        __import__("pv_growth.core.flags", fromlist=["FlagService"]).FlagService(s),
        tg,
        day=date(2026, 10, 12),
        slot=1,
    )
    alias = RawConfig(
        source_id=row.source_id,
        uri_hash="f" * 64,
        raw_uri=row.raw_uri.split("#")[0] + "#another-name",
        protocol=row.protocol,
        host=row.host,
        port=row.port,
    )
    session.add(alias)
    session.commit()
    assert alias.id in config_alias_exclusions(session, [row.id])


def test_replayed_manager_update_cannot_override_a_newer_setting(session, settings):
    m = manager()
    s = settings.model_copy(
        update={"official_channel_id": "@pvnetwork0", "free_channel_id": "@pvnetwork_freeconfig"}
    )
    client = ManagerClient()
    params = dict(user_id=42, chat_id=42, chat_type="private")
    m.handle_action(session, s, client, **params, action="web:10", request_key="m:" + "a" * 40)
    m.handle_action(session, s, client, **params, action="web:off", request_key="m:" + "b" * 40)
    m.handle_action(session, s, client, **params, action="web:10", request_key="m:" + "a" * 40)
    assert session.get(AppConfig, "pv_free_growth_policy").value["community_enabled"] is False


def test_owner_of_free_with_main_admin_rights_can_manage_but_legacy_owner_cannot(settings):
    from pv_growth.free_config.membership import FREE_CHANNEL_ID

    m = manager()
    client = ManagerClient()
    client.get_chat_member = lambda channel, user: {
        "status": "creator" if channel == FREE_CHANNEL_ID else "administrator",
        "user": {"id": user},
    }
    assert m.authorized(settings.model_copy(update={"official_channel_id": "@legacy_other"}), client, 42)
    client.get_chat_member = lambda channel, user: {"status": "member", "user": {"id": user}}
    assert not m.authorized(settings, client, 42)
    client.get_chat_member = lambda channel, user: {"status": "creator", "user": {"id": 43}}
    assert not m.authorized(settings, client, 42)


def test_old_unrecorded_shared_effect_cannot_be_recreated_or_reclassified(session, settings, monkeypatch):
    s, flags, tg, transport = setup(session, settings)
    enable(session)
    row = FreeAllocation(
        id="pv_shared:2026-10-13:1",
        day=date(2026, 10, 13),
        bucket="public",
        traffic_bytes=1024**3,
        status="pending_provision",
    )
    session.add(row)
    session.commit()
    monkeypatch.setattr(shared_public, "local_day", lambda now=None: date(2026, 10, 13))
    backend = Backend()
    assert (
        shared_public.publish_shared_slot(session, s, flags, tg, backend, day=date(2026, 10, 13), slot=1)
        is None
    )
    assert row.status == "pending_provision" and not backend.calls


def test_native_report_does_not_retry_even_an_existing_failed_receipt(session, settings, monkeypatch):
    from pv_growth.database.models import MessageLog
    from pv_growth.telegram import client as telegram

    m = manager()
    client = ManagerClient()
    monkeypatch.setattr(m, "utcnow", lambda: datetime(2026, 10, 13, 20, 25))
    m.handle_action(session, settings, client, user_id=42, chat_id=42, chat_type="private", action="menu")
    monkeypatch.setattr(telegram, "get_telegram", lambda s: client)
    m.daily_report(session, settings, {"day": "2026-10-13"})
    receipt = session.query(MessageLog).filter_by(purpose="native_admin_report").one()
    receipt.status = "failed"
    session.commit()
    sent = len(client.sent)
    m.daily_report(session, settings, {"day": "2026-10-13"})
    assert len(client.sent) == sent
