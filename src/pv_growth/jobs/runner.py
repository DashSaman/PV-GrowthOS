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


def register_builtin_handlers() -> None:
    """Import handler modules so their @handler decorators register.
    Without this the registry is empty at runtime and every job errors."""
    import pv_growth.lifecycle.service  # noqa: F401


def dispatch(session: Session, settings: Settings, job_type: str, payload: dict) -> None:
    register_builtin_handlers()
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
    register_builtin_handlers()
    log.info("job runner started (foreground)")
    while True:
        count = run_tick(settings)
        if count == 0:
            time.sleep(settings.scheduler_interval_seconds)
    return 0


from pv_growth.jobs.scheduler import Scheduler  # noqa: E402,F401 — re-export

