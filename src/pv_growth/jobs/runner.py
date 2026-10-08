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


class RetryableJobError(Exception):
    """Handler state is intentional evidence and must commit before job backoff."""


class DeferredJobError(Exception):
    """Expected cooldown; keep the same job without consuming a retry attempt."""

    def __init__(self, scheduled_at):
        super().__init__("dispatch deferred")
        self.scheduled_at = scheduled_at


def handler(job_type: str):
    def register(fn: Handler) -> Handler:
        _HANDLERS[job_type] = fn
        return fn

    return register


def register_builtin_handlers() -> None:
    """Import handler modules so their @handler decorators register.
    Without this the registry is empty at runtime and every job errors."""
    import pv_growth.acquisition.daily  # noqa: F401
    import pv_growth.content.jobs  # noqa: F401
    import pv_growth.conversions.service  # noqa: F401
    import pv_growth.free_config.jobs  # noqa: F401
    import pv_growth.free_config.lottery  # noqa: F401
    import pv_growth.free_config.shared_public  # noqa: F401
    import pv_growth.lifecycle.service  # noqa: F401
    import pv_growth.mirza_adapter.sync_job  # noqa: F401
    import pv_growth.provisioning.lifecycle_job  # noqa: F401


def dispatch(session: Session, settings: Settings, job_type: str, payload: dict) -> None:
    register_builtin_handlers()
    fn = _HANDLERS.get(job_type)
    if fn is None:
        raise ValueError(f"no handler registered for job_type={job_type}")
    fn(session, settings, payload)


def run_tick(settings: Settings, *, worker_id: str | None = None, batch: int | None = None) -> int:
    """Claim and execute up to `batch` due jobs. Returns executed count."""
    worker = worker_id or f"worker-{threading.get_ident()}"
    effective_batch = batch if batch is not None else settings.job_batch_size
    executed = 0
    while executed < effective_batch:
        with session_scope(settings) as session:
            jobs.recover_stale(session, stale_seconds=settings.job_lock_stale_seconds)
            job = jobs.claim_next(session, worker, stale_seconds=settings.job_lock_stale_seconds)
            if job is None:
                break
            executed += 1
            try:
                dispatch(session, settings, job.job_type, job.payload)
                session.commit()
                jobs.complete(session, job.id)
            except DeferredJobError as exc:
                job.status = "pending"
                job.scheduled_at = exc.scheduled_at
                job.attempt = max(0, job.attempt - 1)
                job.locked_at = job.locked_by = None
                job.retry_after = job.finished_at = job.last_error = None
                session.commit()
            except Exception as exc:  # noqa: BLE001 — job failures are data, not crashes
                log.error(
                    "job execution failed",
                    job_id=job.id,
                    job_type=job.job_type,
                    error=str(exc),
                    trace=traceback.format_exc()[-500:],
                )
                if isinstance(exc, RetryableJobError):
                    session.commit()
                else:
                    session.rollback()
                with session_scope(settings) as fresh:
                    fresh_job = fresh.get(type(job), job.id)
                    if fresh_job is not None:
                        jobs.fail(
                            fresh,
                            fresh_job,
                            str(exc),
                            max_attempts=min(
                                fresh_job.max_attempts,
                                settings.job_max_attempts_default,
                            ),
                        )
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
