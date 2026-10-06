"""PostgreSQL-backed durable job queue (no Redis in v1).

Guarantees:
- jobs survive restarts (durable rows)
- a job is claimed by exactly one worker (atomic UPDATE with lock check)
- duplicate enqueues of the same logical work collapse (unique idempotency_key)
- failures retry with exponential backoff, then dead-end in status=failed
- stale locks (crashed workers) are recovered automatically
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.logging import get_logger
from pv_growth.database.models import Job
from pv_growth.database.types import utcnow

log = get_logger("jobs")

BACKOFF_BASE_SECONDS = 60.0  # attempt n waits base * 2^(n-1), capped below
BACKOFF_CAP_SECONDS = 3600.0


def enqueue(
    session: Session,
    job_type: str,
    payload: dict | None = None,
    *,
    scheduled_at: datetime | None = None,
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
    priority: int = 0,
) -> tuple[Job, bool]:
    """Idempotent enqueue: same idempotency_key returns the existing job."""
    payload = payload or {}
    existing = None
    if idempotency_key:
        existing = session.execute(
            select(Job).where(Job.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    job = Job(
        job_type=job_type,
        payload=payload,
        scheduled_at=scheduled_at or utcnow(),
        idempotency_key=idempotency_key,
        max_attempts=max_attempts or 5,
        priority=priority,
    )
    if not idempotency_key:
        job.idempotency_key = f"auto:{uuid.uuid4().hex}"  # uniqueness, no dedupe
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.execute(
            select(Job).where(Job.idempotency_key == job.idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        raise
    log.info("job enqueued", job_type=job_type, job_id=job.id, idempotency_key=idempotency_key)
    return job, True


def claim_next(
    session: Session, worker_id: str, *, stale_seconds: int, now: datetime | None = None
) -> Job | None:
    """Atomically claim the next due job. Works identically on PostgreSQL and
    SQLite: single-row UPDATE wins exactly one contender."""
    now = now or utcnow()
    stale_before = now - timedelta(seconds=stale_seconds)

    candidates = (
        session.execute(
            select(Job.id)
            .where(
                Job.status.in_(("pending", "running")),
                (Job.retry_after.is_(None)) | (Job.retry_after <= now),
                Job.scheduled_at <= now,
                (Job.locked_at.is_(None)) | (Job.locked_at < stale_before),
            )
            .order_by(Job.priority.desc(), Job.scheduled_at)
            .limit(10)
        )
        .scalars()
        .all()
    )
    if not candidates:
        return None

    for job_id in candidates:
        updated = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                (Job.locked_at.is_(None)) | (Job.locked_at < stale_before),
                Job.status.in_(("pending", "running")),
            )
            .values(locked_at=now, locked_by=worker_id, status="running", attempt=Job.attempt + 1)
        )
        if updated.rowcount == 1:
            session.commit()
            return session.get(Job, job_id)
    return None


def complete(session: Session, job_id: int) -> None:
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status="done", finished_at=utcnow(), locked_at=None, locked_by=None)
    )
    session.commit()


def fail(session: Session, job: Job, error: str, *, max_attempts: int | None = None) -> Job:
    """Record failure; schedule exponential-backoff retry or dead-end the job."""
    effective_max = max_attempts or job.max_attempts
    if job.attempt >= effective_max:
        session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(
                status="failed", finished_at=utcnow(), last_error=error[:2000], locked_at=None, locked_by=None
            )
        )
        log.error("job dead after max attempts", job_id=job.id, job_type=job.job_type, attempt=job.attempt)
    else:
        backoff = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, job.attempt - 1)))
        session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(
                status="pending",
                retry_after=utcnow() + timedelta(seconds=backoff),
                last_error=error[:2000],
                locked_at=None,
                locked_by=None,
            )
        )
        log.warning(
            "job failed, will retry",
            job_id=job.id,
            job_type=job.job_type,
            attempt=job.attempt,
            backoff_seconds=backoff,
        )
    session.commit()
    session.refresh(job)
    return job


def recover_stale(session: Session, *, stale_seconds: int, now: datetime | None = None) -> int:
    """Release locks held past the stale threshold (crashed workers)."""
    now = now or utcnow()
    stale_before = now - timedelta(seconds=stale_seconds)
    result = session.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < stale_before)
        .values(status="pending", locked_at=None, locked_by=None)
    )
    session.commit()
    if result.rowcount:
        log.warning("stale job locks recovered", count=result.rowcount)
    return result.rowcount


def queue_depth(session: Session) -> dict[str, int]:
    out: dict[str, int] = {}
    for status in ("pending", "running", "failed", "done"):
        out[status] = len(session.execute(select(Job.id).where(Job.status == status)).scalars().all())
    return out


def prune_terminal(
    session: Session,
    *,
    done_before: datetime,
    failed_before: datetime,
) -> int:
    """Prune terminal queue history while retaining failed diagnostics longer."""
    result = session.execute(
        delete(Job).where(
            or_(
                (
                    Job.status.in_(("done", "cancelled"))
                    & (Job.finished_at.isnot(None))
                    & (Job.finished_at < done_before)
                ),
                (
                    (Job.status == "failed")
                    & (Job.finished_at.isnot(None))
                    & (Job.finished_at < failed_before)
                ),
            )
        )
    )
    return int(result.rowcount or 0)
