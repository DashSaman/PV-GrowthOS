"""Phase 3 acceptance: durable jobs — idempotent enqueue, exclusive claim,
retry/backoff, stale recovery, and no-duplicate-effects under double runs."""

import threading
import time
from datetime import timedelta

import pytest

from pv_growth.database.models import Job
from pv_growth.database.types import utcnow
from pv_growth.jobs import service as jobs


@pytest.fixture(autouse=True)
def _clear_queue(session):
    """Cancel every pre-existing pending/running job so queue tests are isolated
    (the test database is shared across the whole pytest session)."""
    session.query(Job).filter(Job.status.in_(("pending", "running"))).update(
        {"status": "cancelled"}, synchronize_session=False
    )
    session.commit()


def test_enqueue_idempotent(session):
    job1, created1 = jobs.enqueue(session, "test.ping", {"x": 1}, idempotency_key="ping:1")
    job2, created2 = jobs.enqueue(session, "test.ping", {"x": 1}, idempotency_key="ping:1")
    assert created1 is True and created2 is False
    assert job1.id == job2.id
    assert session.query(Job).filter_by(job_type="test.ping").count() == 1


def test_claim_exclusive_two_workers(session):
    job, _ = jobs.enqueue(session, "test.exclusive", {}, idempotency_key="ex:1")
    first = jobs.claim_next(session, "worker-a", stale_seconds=300)
    assert first is not None and first.id == job.id
    # drain everything else that is due (leftovers from other tests share the DB)
    claimed_ids = [first.id]
    while (other := jobs.claim_next(session, "worker-b", stale_seconds=300)) is not None:
        claimed_ids.append(other.id)
    # the guarded job was claimed exactly once across all workers
    assert claimed_ids.count(job.id) == 1  # exactly one worker owns the job


def test_fail_retries_with_backoff_then_deadends(session):
    job, _ = jobs.enqueue(session, "test.retry", {}, idempotency_key="rt:1", max_attempts=2)
    claimed = jobs.claim_next(session, "w1", stale_seconds=300)
    assert claimed is not None
    jobs.fail(session, claimed, "boom")
    assert claimed.status == "pending" and claimed.retry_after > utcnow()

    # simulate backoff elapsed; claim again and fail -> dead (attempt 2 >= max 2)
    claimed.retry_after = utcnow() - timedelta(seconds=1)
    session.commit()
    again = jobs.claim_next(session, "w1", stale_seconds=300)
    assert again is not None and again.attempt == 2
    jobs.fail(session, again, "boom again")
    assert again.status == "failed" and again.finished_at is not None


def test_stale_lock_recovery(session):
    job, _ = jobs.enqueue(session, "test.stale", {}, idempotency_key="st:1")
    # simulate a crashed worker: locked long ago
    job.status = "running"
    job.locked_at = utcnow() - timedelta(hours=1)
    job.locked_by = "ghost"
    session.commit()

    recovered = jobs.recover_stale(session, stale_seconds=300)
    assert recovered >= 1
    fresh = jobs.claim_next(session, "w2", stale_seconds=300)
    assert fresh is not None and fresh.id == job.id


def test_runner_executes_each_job_once_under_double_run(settings, session, monkeypatch):
    from pv_growth.jobs import runner

    calls: list[dict] = []

    @runner.handler("test.count")
    def _count(session_, settings_, payload_):
        calls.append(payload_)

    jobs.enqueue(session, "test.count", {"n": 1}, idempotency_key="c:1")
    session.commit()  # real flow: ingest commits, THEN the runner (own session) sees it
    # two ticks back to back (second finds nothing due)
    executed1 = runner.run_tick(settings, worker_id="t1")
    executed2 = runner.run_tick(settings, worker_id="t2")
    assert executed1 == 1 and executed2 == 0
    assert len(calls) == 1  # acceptance: duplicate scheduler runs never duplicate effects


def test_queue_depth(settings, session):
    jobs.enqueue(session, "test.d1", {}, idempotency_key="d:1")
    jobs.enqueue(session, "test.d2", {}, idempotency_key="d:2")
    depth = jobs.queue_depth(session)
    assert depth["pending"] >= 2


def test_app_starts_with_scheduler_enabled(settings, monkeypatch):
    """Regression: PVG_SCHEDULER_ENABLED=1 must not crash startup
    (caught a missing module in the first production image)."""
    from fastapi.testclient import TestClient

    enabled = settings.model_copy(update={"scheduler_enabled": True})
    monkeypatch.setattr("pv_growth.main.get_settings", lambda: enabled)
    from pv_growth.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/health").status_code == 200


def test_builtin_handlers_register_and_scan_job_completes(settings, session):
    """Regression: handler modules must be imported so the registry is
    populated (first production deploy left lifecycle.scan jobs pending)."""
    from pv_growth.jobs import runner

    runner.register_builtin_handlers()
    assert "lifecycle.scan" in runner._HANDLERS

    jobs.enqueue(session, "lifecycle.scan", {}, idempotency_key="scan:1")
    session.commit()
    executed = runner.run_tick(settings, worker_id="t3")
    assert executed == 1
    from pv_growth.database.models import Job

    done = session.query(Job).filter_by(idempotency_key="scan:1").one()
    assert done.status == "done"


def test_content_publish_job_is_registered():
    from pv_growth.jobs import runner

    runner.register_builtin_handlers()

    assert "content.publish_due" in runner._HANDLERS


def test_content_scheduler_enqueue_is_deduplicated(settings, session):
    from pv_growth.jobs.scheduler import Scheduler

    scheduler = Scheduler(settings)
    scheduler._enqueue_periodic()
    scheduler._enqueue_periodic()

    rows = session.query(Job).filter_by(job_type="content.publish_due").all()
    assert len(rows) == 1
    assert rows[0].idempotency_key.startswith("content_publish:")


def test_runner_uses_configured_batch_size(settings, session):
    from pv_growth.jobs import runner

    seen = []

    @runner.handler("test.configured_batch")
    def _configured_batch(session_, settings_, payload_):
        seen.append(payload_["n"])

    for n in range(3):
        jobs.enqueue(
            session,
            "test.configured_batch",
            {"n": n},
            idempotency_key=f"configured-batch:{n}",
        )
    session.commit()

    bounded = settings.model_copy(update={"job_batch_size": 2})
    assert runner.run_tick(bounded, worker_id="configured-batch") == 2
    assert len(seen) == 2


def test_runner_uses_configured_default_max_attempts(settings, session):
    from pv_growth.jobs import runner

    @runner.handler("test.configured_attempts")
    def _always_fail(session_, settings_, payload_):
        raise RuntimeError("expected failure")

    jobs.enqueue(
        session,
        "test.configured_attempts",
        {},
        idempotency_key="configured-attempts",
    )
    session.commit()
    bounded = settings.model_copy(update={"job_max_attempts_default": 1})

    assert runner.run_tick(bounded, worker_id="configured-attempts") == 1
    session.expire_all()
    row = session.query(Job).filter_by(idempotency_key="configured-attempts").one()
    assert row.status == "failed"
    assert row.attempt == 1


def test_terminal_retention_preserves_failed_diagnostics_longer(session):
    old = utcnow() - timedelta(days=40)
    for status in ("done", "cancelled", "failed"):
        session.add(
            Job(
                job_type=f"retention.{status}",
                payload={},
                status=status,
                idempotency_key=f"retention:{status}",
                finished_at=old,
            )
        )
    session.flush()

    removed = jobs.prune_terminal(
        session,
        done_before=utcnow() - timedelta(days=7),
        failed_before=utcnow() - timedelta(days=90),
    )

    assert removed == 2
    assert session.query(Job).filter_by(idempotency_key="retention:failed").count() == 1


def test_scheduler_polling_worker_does_not_block_periodic_ticks(settings, monkeypatch):
    from pv_growth.jobs import runner
    from pv_growth.jobs.scheduler import Scheduler
    from pv_growth.telegram.poller import TelegramPoller

    tick_count = 0
    tick_lock = threading.Lock()
    poll_started = threading.Event()

    def _tick(self):
        nonlocal tick_count
        with tick_lock:
            tick_count += 1

    def _run_tick(*args, **kwargs):
        return 0

    def _slow_poll(self):
        poll_started.set()
        time.sleep(0.15)
        return 0

    monkeypatch.setattr(Scheduler, "_enqueue_periodic", _tick)
    monkeypatch.setattr(runner, "run_tick", _run_tick)
    monkeypatch.setattr(TelegramPoller, "poll_once", _slow_poll)
    configured = settings.model_copy(
        update={
            "scheduler_enabled": True,
            "telegram_polling_enabled": True,
            "telegram_bot_token": "test-token",
            "scheduler_interval_seconds": 0.01,
        }
    )
    scheduler = Scheduler(configured)
    scheduler.start()
    try:
        assert poll_started.wait(0.5)
        time.sleep(0.06)
        with tick_lock:
            assert tick_count >= 3
    finally:
        scheduler.stop()
