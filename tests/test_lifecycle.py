"""Phase 3 acceptance: lifecycle guard rails — purchase stops reminders,
cooldown, max sends, idempotent sends; segments recalculable; strict templates."""

import uuid

import httpx
import pytest
from sqlalchemy import event

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import LifecycleRule, MessageLog, MessageTemplate
from pv_growth.events.service import get_or_create_user, ingest
from pv_growth.lifecycle.service import scan, send_followup
from pv_growth.messaging import service as messaging
from pv_growth.segments.service import compute_segment, recompute_all
from pv_growth.telegram.client import (
    FakeTelegramTransport,
    HttpTelegramTransport,
    TelegramClient,
    TelegramDeliveryUnknownError,
    TelegramRetryableError,
)


def _flags_on(settings: Settings) -> FlagService:
    return FlagService(settings.model_copy(update={"flag_lifecycle_automation_enabled": True}))


@pytest.fixture
def msg_env(settings, session):
    code = uuid.uuid4().hex[:8]
    session.add(
        MessageTemplate(
            code=f"tpl_{code}",
            intent="trial_followup",
            body="سلام {{first_name}}! تست {{traffic_gb}} گیگ شما آماده است.",
        )
    )
    session.add(
        LifecycleRule(
            code=f"rule_{code}",
            trigger="trial_no_purchase",
            template_code=f"tpl_{code}",
            cooldown_hours=24,
            max_sends=1,
            stop_conditions=["PAYMENT_SUCCESS"],
        )
    )
    session.flush()
    tg = TelegramClient(FakeTelegramTransport(), settings)
    return {"code": code, "tg": tg, "tpl": f"tpl_{code}", "rule": f"rule_{code}"}


def _user_with_trial(session, tg_id, chat=True):
    user, _ = get_or_create_user(
        session, telegram_user_id=tg_id, first_name="Test", telegram_chat_id=tg_id if chat else None
    )
    ingest(session, "BOT_STARTED", user_id=user.id, idempotency_key=f"bs:{tg_id}")
    ingest(session, "TRIAL_CREATED", user_id=user.id, idempotency_key=f"tc:{tg_id}")
    ingest(session, "TRIAL_CONNECTED", user_id=user.id, idempotency_key=f"tconn:{tg_id}")
    session.flush()
    return user


def test_purchase_stops_sales_reminder(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8101)
    # user converts BEFORE the followup fires
    ingest(
        session,
        "PAYMENT_SUCCESS",
        user_id=user.id,
        idempotency_key=f"pay:{user.id}",
        metadata={"order_id": "o1", "amount_cents": 100},
    )
    session.flush()

    status = send_followup(
        session, settings, flags, msg_env["tg"], {"rule_code": msg_env["rule"], "user_id": user.id}
    )
    assert status == "stopped"  # acceptance: purchasers stop receiving wrong reminders
    assert session.query(MessageLog).count() == 0
    assert msg_env["tg"]._t.calls == []


def test_send_respects_cooldown_and_max_sends(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8102)

    payload = {"rule_code": msg_env["rule"], "user_id": user.id, "facts": {"traffic_gb": 5}}
    first = send_followup(session, settings, flags, msg_env["tg"], payload)
    assert first == "sent"

    # immediate second run: blocked by max_sends=1 (and cooldown 24h)
    second = send_followup(session, settings, flags, msg_env["tg"], payload)
    assert second in {"cooldown", "max_sends", "duplicate"}
    sent = [t for m, t in []]  # noqa: F841
    texts = [p.get("text", "") for m, p in msg_env["tg"]._t.calls if m == "sendMessage"]
    assert len(texts) == 1  # exactly one message ever


def test_max_sends_two_uses_two_stable_ordinals(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8112)
    rule = session.query(LifecycleRule).filter_by(code=msg_env["rule"]).one()
    rule.max_sends = 2
    rule.cooldown_hours = 0
    session.flush()

    payload = {"rule_code": rule.code, "user_id": user.id, "facts": {"traffic_gb": 5}}
    assert send_followup(session, settings, flags, msg_env["tg"], payload) == "sent"
    assert send_followup(session, settings, flags, msg_env["tg"], payload) == "sent"
    assert send_followup(session, settings, flags, msg_env["tg"], payload) == "max_sends"

    rows = session.query(MessageLog).filter_by(purpose=rule.code, user_id=user.id).all()
    assert [(row.send_ordinal, row.status) for row in rows] == [(1, "sent"), (2, "sent")]
    template = session.query(MessageTemplate).filter_by(code=rule.template_code).one()
    assert {row.dedupe_key for row in rows} == {
        f"lc:{rule.code}:{user.id}:1:v{template.version}",
        f"lc:{rule.code}:{user.id}:2:v{template.version}",
    }


def test_purchase_stop_condition_is_rechecked_before_second_send(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8113)
    rule = session.query(LifecycleRule).filter_by(code=msg_env["rule"]).one()
    rule.max_sends = 2
    rule.cooldown_hours = 0
    session.flush()
    payload = {"rule_code": rule.code, "user_id": user.id, "facts": {"traffic_gb": 5}}

    assert send_followup(session, settings, flags, msg_env["tg"], payload) == "sent"
    ingest(
        session,
        "PAYMENT_SUCCESS",
        user_id=user.id,
        idempotency_key=f"pay:second:{user.id}",
    )
    session.flush()

    assert send_followup(session, settings, flags, msg_env["tg"], payload) == "stopped"
    assert len(msg_env["tg"]._t.sent_texts()) == 1


class _FailingTransport:
    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    def call(self, method: str, payload: dict) -> dict:
        self.calls += 1
        raise self.error


def test_definite_transport_failure_is_failed_and_retryable(settings, session, msg_env):
    user = _user_with_trial(session, 8114)
    transport = _FailingTransport(TelegramRetryableError("connect failed"))
    telegram = TelegramClient(transport, settings)

    with pytest.raises(TelegramRetryableError):
        messaging.send_user_message(
            session,
            telegram,
            user_id=user.id,
            template_code=msg_env["tpl"],
            purpose="retryable",
            facts={"traffic_gb": 5},
            dedupe_key=f"retryable:{user.id}:1",
            send_ordinal=1,
        )

    row = session.query(MessageLog).filter_by(dedupe_key=f"retryable:{user.id}:1").one()
    assert row.status == "failed"
    assert row.attempts == 1
    assert row.sent_at is None


def test_lifecycle_job_persists_failed_delivery_and_retries(settings, session, msg_env, monkeypatch):
    from pv_growth.database.models import Job
    from pv_growth.jobs import runner
    from pv_growth.jobs import service as jobs

    user = _user_with_trial(session, 8116)
    transport = _FailingTransport(TelegramRetryableError("connect failed"))
    telegram = TelegramClient(transport, settings)
    session.query(Job).filter(Job.status.in_(("pending", "running"))).update(
        {"status": "cancelled"}, synchronize_session=False
    )
    jobs.enqueue(
        session,
        "lifecycle.send_followup",
        {
            "rule_code": msg_env["rule"],
            "user_id": user.id,
            "facts": {"traffic_gb": 5},
            "send_ordinal": 1,
        },
        idempotency_key=f"lc:{msg_env['rule']}:{user.id}:1:v1",
        max_attempts=2,
    )
    session.commit()
    monkeypatch.setattr(
        "pv_growth.lifecycle.service.FlagService",
        lambda _settings: _flags_on(settings),
    )
    monkeypatch.setattr(
        "pv_growth.lifecycle.service._telegram_or_fail",
        lambda _settings: telegram,
    )

    assert runner.run_tick(settings, worker_id="lifecycle-retry") == 1

    session.expire_all()
    job = session.query(Job).filter_by(idempotency_key=f"lc:{msg_env['rule']}:{user.id}:1:v1").one()
    row = session.query(MessageLog).filter_by(purpose=msg_env["rule"], user_id=user.id).one()
    assert job.status == "pending"
    assert job.attempt == 1
    assert "connect failed" in (job.last_error or "")
    assert row.status == "failed"
    assert row.attempts == 1


def test_blocked_lifecycle_job_is_terminal_noop(settings, session, msg_env, monkeypatch):
    from pv_growth.lifecycle import service as lifecycle

    user = _user_with_trial(session, 8117)
    user.is_blocked = True
    session.flush()
    monkeypatch.setattr(
        "pv_growth.lifecycle.service.FlagService",
        lambda _settings: _flags_on(settings),
    )
    monkeypatch.setattr(
        "pv_growth.lifecycle.service._telegram_or_fail",
        lambda _settings: msg_env["tg"],
    )

    lifecycle._followup_job(
        session,
        settings,
        {
            "rule_code": msg_env["rule"],
            "user_id": user.id,
            "send_ordinal": 1,
        },
    )

    assert not msg_env["tg"]._t.sent_texts()


def test_ambiguous_delivery_is_unknown_and_never_blindly_resent(settings, session, msg_env):
    user = _user_with_trial(session, 8115)
    transport = _FailingTransport(TelegramDeliveryUnknownError("read timed out"))
    telegram = TelegramClient(transport, settings)
    kwargs = {
        "user_id": user.id,
        "template_code": msg_env["tpl"],
        "purpose": "unknown",
        "facts": {"traffic_gb": 5},
        "dedupe_key": f"unknown:{user.id}:1",
        "send_ordinal": 1,
    }

    row, attempted = messaging.send_user_message(session, telegram, **kwargs)
    again, attempted_again = messaging.send_user_message(session, telegram, **kwargs)

    assert attempted is True
    assert attempted_again is False
    assert again.id == row.id
    assert row.status == "delivery_unknown"
    assert row.attempts == 1
    assert transport.calls == 1


def test_lifecycle_reports_unknown_delivery_without_resend(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8117)
    transport = _FailingTransport(TelegramDeliveryUnknownError("read timed out"))
    telegram = TelegramClient(transport, settings)
    payload = {
        "rule_code": msg_env["rule"],
        "user_id": user.id,
        "facts": {"traffic_gb": 5},
    }

    assert send_followup(session, settings, flags, telegram, payload) == "delivery_unknown"
    assert send_followup(session, settings, flags, telegram, payload) == "max_sends"
    assert transport.calls == 1


@pytest.mark.parametrize(
    ("transport_error", "expected_error"),
    [
        (httpx.ConnectError("connect failed"), TelegramRetryableError),
        (httpx.ReadTimeout("read timed out"), TelegramDeliveryUnknownError),
    ],
)
def test_http_transport_classifies_delivery_safety(settings, transport_error, expected_error):
    configured = settings.model_copy(update={"telegram_bot_token": "test-token"})
    transport = HttpTelegramTransport(configured)

    class _HttpClient:
        def post(self, url, json):
            raise transport_error

    transport._client = _HttpClient()
    with pytest.raises(expected_error):
        transport.call("sendMessage", {"chat_id": 1, "text": "hi"})


def test_send_is_idempotent_on_dedupe_key(settings, session, msg_env):
    user = _user_with_trial(session, 8103)
    row1, created1 = messaging.send_user_message(
        session,
        msg_env["tg"],
        user_id=user.id,
        template_code=msg_env["tpl"],
        purpose="manual",
        facts={"traffic_gb": 5},
        dedupe_key=f"m:{user.id}:1",
    )
    row2, created2 = messaging.send_user_message(
        session,
        msg_env["tg"],
        user_id=user.id,
        template_code=msg_env["tpl"],
        purpose="manual",
        facts={"traffic_gb": 5},
        dedupe_key=f"m:{user.id}:1",
    )
    assert created1 is True and created2 is False
    assert row1.id == row2.id


def test_scan_schedules_jobs_idempotently(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8104)
    scheduled1 = scan(session, settings, flags)
    scan(session, settings, flags)  # second scan: idempotent enqueue
    assert scheduled1 >= 1
    # second scan finds the same candidates but enqueue is idempotent
    from pv_growth.database.models import Job

    count = session.query(Job).filter(Job.idempotency_key == f"lc:{msg_env['rule']}:{user.id}:1:v1").count()
    assert count == 1


def test_scan_disabled_by_flag(settings, session, msg_env):
    flags = FlagService(settings)  # default OFF
    _user_with_trial(session, 8105)
    assert scan(session, settings, flags) == 0


def test_template_render_strict():
    body = "قیمت {{price}} تومان، حجم {{traffic_gb}} گیگ، اعتبار {{validity_hours}} ساعت"
    ok = messaging.render(body, {"price": 99000, "traffic_gb": 10, "validity_hours": 30})
    assert "99000" in ok
    with pytest.raises(ValidationError):  # missing fact -> refuse, never invent
        messaging.render(body, {"price": 99000})
    # malformed placeholder refused
    with pytest.raises(ValidationError):
        messaging.render("این {price} خراب است {{price}", {"price": 1})


def test_segments_recomputable(session):
    user, _ = get_or_create_user(session, telegram_user_id=8201)
    ingest(session, "BOT_STARTED", user_id=user.id, idempotency_key="s1:bs")
    session.flush()
    assert compute_segment(session, user) == "NEW_LEAD"

    ingest(session, "TRIAL_CREATED", user_id=user.id, idempotency_key="s1:tc")
    ingest(session, "TRIAL_CONNECTED", user_id=user.id, idempotency_key="s1:tconn")
    ingest(session, "PRICING_VIEWED", user_id=user.id, idempotency_key="s1:pv")
    session.flush()
    assert compute_segment(session, user) == "HOT_LEAD"

    ingest(session, "PAYMENT_SUCCESS", user_id=user.id, idempotency_key="s1:pay")
    session.flush()
    assert compute_segment(session, user) == "FIRST_TIME_BUYER"

    ingest(session, "PAYMENT_SUCCESS", user_id=user.id, idempotency_key="s1:pay2", metadata={"renewal": True})
    ingest(session, "SERVICE_RENEWED", user_id=user.id, idempotency_key="s1:ren")
    session.flush()
    assert compute_segment(session, user) == "RETURNING_CUSTOMER"

    counts = recompute_all(session)
    assert counts.get("RETURNING_CUSTOMER", 0) >= 1


def test_candidate_scan_query_count_is_bounded(session):
    from pv_growth.lifecycle.triggers import candidate_users

    for offset in range(20):
        _user_with_trial(session, 8300 + offset)
    session.flush()
    statements = 0

    def _count_select(conn, cursor, statement, parameters, context, executemany):
        nonlocal statements
        if statement.lstrip().upper().startswith("SELECT"):
            statements += 1

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", _count_select)
    try:
        rows = candidate_users(session, "trial_no_purchase", limit=10)
    finally:
        event.remove(bind, "before_cursor_execute", _count_select)

    assert len(rows) == 10
    assert statements <= 2


def test_segment_recompute_prefetches_population_in_batches(session):
    for offset in range(25):
        user, _ = get_or_create_user(session, telegram_user_id=8400 + offset)
        ingest(
            session,
            "BOT_STARTED",
            user_id=user.id,
            idempotency_key=f"bounded-segment:{offset}",
        )
    session.flush()
    statements = 0

    def _count_select(conn, cursor, statement, parameters, context, executemany):
        nonlocal statements
        if statement.lstrip().upper().startswith("SELECT"):
            statements += 1

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", _count_select)
    try:
        counts = recompute_all(session, batch_size=100)
    finally:
        event.remove(bind, "before_cursor_execute", _count_select)

    assert sum(counts.values()) >= 25
    assert statements <= 5
