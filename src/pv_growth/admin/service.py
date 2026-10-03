"""Admin operations service: system health view used by the dashboard."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.database.models import Event, Job
from pv_growth.jobs import service as jobs


def system_health(session: Session) -> dict:
    """Health snapshot: queue, failures, event volume — CLI/API share this."""
    depth = jobs.queue_depth(session)
    failed = session.execute(
        select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(10)
    ).scalars().all()
    latest = session.execute(
        select(Event).order_by(Event.id.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "queue": depth,
        "failed_jobs": [
            {"id": j.id, "type": j.job_type, "attempts": j.attempt,
             "error": (j.last_error or "")[:160]}
            for j in failed
        ],
        "last_event_at": latest.occurred_at.isoformat() if latest else None,
        "healthy": depth["failed"] == 0,
    }
