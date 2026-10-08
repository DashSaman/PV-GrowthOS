from datetime import UTC, timedelta

import pytest
from sqlalchemy import select

from pv_growth.core.flags import FlagService
from pv_growth.database.base import session_scope
from pv_growth.database.models import LifecycleRule, MessageLog, MessageTemplate
from pv_growth.database.types import utcnow
from pv_growth.events.service import get_or_create_user, ingest
from pv_growth.lifecycle import service, triggers
from pv_growth.messaging import service as messaging


def setup(session, settings, *, cutoff=None):
    user, _ = get_or_create_user(session, telegram_user_id=124, telegram_chat_id=124)
    session.add(
        MessageTemplate(
            code="pv_trial_offer", intent="trial_followup", body="برای خرید به ربات رسمی مراجعه کن."
        )
    )
    rule = LifecycleRule(
        code="pv_trial_received",
        trigger="trial_received_no_purchase",
        template_code="pv_trial_offer",
        conditions={
            "not_before_utc": cutoff or (utcnow() - timedelta(hours=1)).replace(tzinfo=UTC).isoformat(),
            "marketing_cooldown_hours": 24,
        },
        stop_conditions=["PAYMENT_SUCCESS"],
    )
    session.add(rule)
    session.commit()
    flags = FlagService(settings.model_copy(update={"flag_lifecycle_automation_enabled": True}))
    return user, rule, flags


def test_receipt_trigger_does_not_invent_connectivity(session, settings):
    user, rule, _ = setup(session, settings)
    ingest(session, "TRIAL_CREATED", user_id=user.id, idempotency_key="trial")
    session.flush()
    assert not triggers.matches_trigger(session, user, rule.trigger)
    ingest(session, "TRIAL_DELIVERED", user_id=user.id, idempotency_key="delivered")
    session.flush()
    assert triggers.matches_trigger(session, user, rule.trigger)
    assert triggers.candidate_users(session, rule.trigger)[0][0].id == user.id


def test_cutoff_excludes_historical_and_bad_cutoff_fails_closed(session, settings):
    user, rule, flags = setup(session, settings)
    rule.trigger = "started_no_trial"
    ingest(
        session,
        "BOT_STARTED",
        user_id=user.id,
        idempotency_key="old",
        occurred_at=utcnow() - timedelta(days=2),
    )
    session.flush()
    assert service.scan(session, settings, flags) == 0
    rule.conditions = {"not_before_utc": "invalid"}
    assert service.scan(session, settings, flags) == 0


def test_send_rechecks_cutoff_and_global_invite_cooldown(session, settings):
    user, rule, flags = setup(session, settings)
    ingest(session, "TRIAL_DELIVERED", user_id=user.id, idempotency_key="trial")
    session.add(
        MessageLog(
            user_id=user.id,
            template_code="existing",
            purpose="never_buyer_invite_20261008",
            dedupe_key="old-invite",
            status="delivery_unknown",
            sent_at=utcnow(),
        )
    )
    session.commit()

    class Telegram:
        def send_message(self, *args):
            pytest.fail("a recent invitation must suppress a followup")

    assert (
        service.send_followup(
            session, settings, flags, Telegram(), {"rule_code": rule.code, "user_id": user.id}
        )
        == "cooldown"
    )


def test_reservation_survives_process_interruption_before_result(session, settings):
    user, _, _ = setup(session, settings)
    # sqlite SAVEPOINT without a real outer BEGIN can accidentally commit a
    # reservation itself. Exercise the real rollback boundary as PostgreSQL does.
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN")

    class Interrupted(BaseException):
        pass

    class Telegram:
        def send_message(self, *args):
            with session_scope(settings) as other:
                assert other.scalar(select(MessageLog).where(MessageLog.dedupe_key == "durable")) is not None
            raise Interrupted()

    with pytest.raises(Interrupted):
        messaging.send_user_message(
            session,
            Telegram(),
            user_id=user.id,
            template_code="pv_trial_offer",
            purpose="pv_trial_received",
            dedupe_key="durable",
        )
    session.rollback()
    assert session.scalar(select(MessageLog).where(MessageLog.dedupe_key == "durable")).status == "reserved"
    row, sent = messaging.send_user_message(
        session,
        Telegram(),
        user_id=user.id,
        template_code="pv_trial_offer",
        purpose="pv_trial_received",
        dedupe_key="durable",
    )
    assert not sent and row.status == "reserved"


def test_cooldown_defers_enqueue_until_it_can_run_instead_of_consuming_job(session, settings):
    user, rule, flags = setup(session, settings)
    ingest(session, "TRIAL_DELIVERED", user_id=user.id, idempotency_key="trial")
    log = MessageLog(
        user_id=user.id,
        template_code="old",
        purpose="never_buyer_invite_20261008",
        dedupe_key="old-invite",
        status="sent",
        sent_at=utcnow(),
    )
    session.add(log)
    session.commit()
    assert service.scan(session, settings, flags) == 0
    log.sent_at = utcnow() - timedelta(hours=25)
    session.commit()
    assert service.scan(session, settings, flags) == 1


def test_technical_trial_delivery_does_not_count_as_a_marketing_message(session, settings):
    user, rule, flags = setup(session, settings)
    ingest(session, "TRIAL_DELIVERED", user_id=user.id, idempotency_key="trial")
    session.add(
        MessageLog(
            user_id=user.id,
            template_code="delivery",
            purpose="free_config",
            dedupe_key="technical-delivery",
            status="sent",
            sent_at=utcnow(),
        )
    )
    session.commit()
    assert not service.marketing_cooldown(session, user.id, 24)


def test_queued_followup_defers_same_job_when_invite_arrives_after_scan(session, settings, monkeypatch):
    from pv_growth.database.models import Job
    from pv_growth.jobs import runner

    user, rule, flags = setup(session, settings)
    ingest(session, "TRIAL_DELIVERED", user_id=user.id, idempotency_key="trial")
    rule.delay_minutes = 0
    session.commit()
    assert service.scan(session, settings, flags) == 1
    session.add(
        MessageLog(
            user_id=user.id,
            template_code="old",
            purpose="never_buyer_invite_20261008",
            dedupe_key="old",
            status="sent",
            sent_at=utcnow(),
        )
    )
    session.commit()
    monkeypatch.setattr(service, "_telegram_or_fail", lambda *a: object())
    monkeypatch.setattr(service, "FlagService", lambda *a: flags)
    assert runner.run_tick(settings, batch=1) == 1
    job = session.query(Job).execution_options(populate_existing=True).one()
    assert job.status == "pending" and job.attempt == 0
    assert job.scheduled_at > utcnow() + timedelta(hours=23)
    assert job.finished_at is None and job.locked_by is None


def test_postgres_cross_purpose_cooldown_serializes_check_and_reservation(session, settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    if not settings.database_url.startswith("postgresql"):
        pytest.skip("shared user-row lock proof runs in PostgreSQL CI")
    user, rule, flags = setup(session, settings)
    ingest(session, "TRIAL_DELIVERED", user_id=user.id, idempotency_key="trial")
    session.add(
        LifecycleRule(
            code="pv_other_offer",
            trigger=rule.trigger,
            template_code=rule.template_code,
            conditions=dict(rule.conditions),
            stop_conditions=["PAYMENT_SUCCESS"],
        )
    )
    session.commit()
    user_id = user.id
    barrier = Barrier(2)

    class Telegram:
        def send_message(self, *args):
            return {"message_id": 1}

    def send(code):
        with session_scope(settings) as other:
            barrier.wait(timeout=10)
            return service.send_followup(
                other, settings, flags, Telegram(), {"rule_code": code, "user_id": user_id}
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, code) for code in [rule.code, "pv_other_offer"]]
        assert sorted(f.result(timeout=20) for f in futures) == ["cooldown", "sent"]
    assert session.query(MessageLog).count() == 1
