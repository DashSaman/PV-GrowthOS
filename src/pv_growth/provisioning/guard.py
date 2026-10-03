"""Resource guard — fail CLOSED before any free provisioning.

Checks (all must pass): database reachable, provisioning backend healthy,
campaign quota (checked in claim_eligibility), and the daily free budget.
GrowthOS itself is hard-capped by cgroup limits (384M / 0.75 CPU / 200 pids),
so host protection is structural; this guard adds the dynamic conditions.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger
from pv_growth.database.models import ExclusiveClaim

log = get_logger("guard")


class GuardDenied(Exception):
    """Provisioning refused — fail closed with a reason."""


def claims_today(session: Session, day: date | None = None) -> int:
    day = day or date.today()
    return session.execute(
        select(func.count()).select_from(ExclusiveClaim).where(
            ExclusiveClaim.claim_date == day,
            ExclusiveClaim.status.in_(("active", "pending_provision")),
        )
    ).scalar_one()


def check(session: Session, settings: Settings, adapter) -> None:
    """Raise GuardDenied when any condition fails. Never retries."""
    # database availability
    from sqlalchemy import text
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise GuardDenied("database unavailable") from exc

    # provisioning backend health
    if adapter is None:
        raise GuardDenied("provisioning backend not configured")
    try:
        if not adapter.health():
            raise GuardDenied("provisioning backend unhealthy")
    except GuardDenied:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GuardDenied(f"provisioning health check failed: {type(exc).__name__}") from exc

    # daily free budget
    used = claims_today(session)
    if used >= settings.free_daily_budget:
        raise GuardDenied(f"daily free budget exhausted ({used}/{settings.free_daily_budget})")

    log.info("provision guard passed", used_today=used,
             budget=settings.free_daily_budget)
