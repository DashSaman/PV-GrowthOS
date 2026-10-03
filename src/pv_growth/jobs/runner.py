"""Job handlers registry + runner loop.

Handlers are pure functions(session, settings, payload) -> None. The runner
claims jobs one at a time (atomic claim) so two runners — or a duplicate
scheduler — never execute the same job twice; effect-level idempotency keys
guard against re-enqueueing the same logical work.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable

from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger
from pv_growth.database.base import session_scope
from pv_growth.jobs import service as jobs

log = get_logger("runner")

Handler = Callable[[Session, Settings, dict], None]
_HANDLERS: dict[str, Handler] = {}


def handler(job_type: str):
    def register(fn: Handler) -> Handler:
        _HANDLERS[job_type] = fn
        return fn
    return register


def dispatch(session: Session, settings: Settings, job_type: str, payload: dict) -> None:
    fn = _HANDLERS.get(job_type)
    if fn is None:
        raise ValueError(f"no handler registered for job_type={job_type}")
    fn(session, settings, payload)


def run_tick(settings: Settings, *, worker_id: str | None = None, batch: int = 10) -> int:
    """Claim and execute up to `batch` due jobs. Returns executed count."""
    worker = worker_id or f"worker-{threading.get_ident()}"
    executed = 0
    while executed < batch:
        with session_scope(settings) as session:
            jobs.recover_stale(session, stale_seconds=settings.job_lock_stale_seconds)
            job = jobs.claim_next(session, worker,
                                  stale_seconds=settings.job_lock_stale_seconds)
            if job is None:
                break
            executed += 1
            try:
                dispatch(session, settings, job.job_type, job.payload)
                session.commit()
                jobs.complete(session, job.id)
            except Exception as exc:  # noqa: BLE001 — job failures are data, not crashes
                log.error("job execution failed", job_id=job.id,
                          job_type=job.job_type, error=str(exc),
                          trace=traceback.format_exc()[-500:])
                session.rollback()
                with session_scope(settings) as fresh:
                    fresh_job = fresh.get(type(job), job.id)
                    if fresh_job is not None:
                        jobs.fail(fresh, fresh_job, str(exc))
    return executed


def run_forever() -> int:
    settings = Settings()
    log.info("job runner started (foreground)")
    while True:
        count = run_tick(settings)
        if count == 0:
            time.sleep(settings.scheduler_interval_seconds)
    return 0


class Scheduler:
    """Background thread started by the app lifespan: enqueues periodic scans
    and drains the queue. Idempotent by design (dedup keys + atomic claims)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        settings = self._settings
        log.info("scheduler loop started", interval=settings.scheduler_interval_seconds)
        while not self._stop.is_set():
            try:
                self._enqueue_periodic()
                run_tick(settings, worker_id="scheduler")
            except Exception as exc:  # noqa: BLE001 — the loop itself must never die
                log.error("scheduler tick failed", error=str(exc))
            self._stop.wait(settings.scheduler_interval_seconds)
        log.info("scheduler loop stopped")

    def _enqueue_periodic(self) -> None:
        from pv_growth.database.base import session_scope

        with session_scope(self._settings) as session:
            # lifecycle scan every interval; dedupe key collapses overlapping ticks
            bucket = int(time.time() // self._settings.scheduler_interval_seconds)
            jobs.enqueue(session, "lifecycle.scan", {},
                         idempotency_key=f"lifecycle_scan:{bucket}")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="pv-growth-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
