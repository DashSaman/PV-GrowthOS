"""Scheduled Mirza sync job: pull new invoices (read-only) into events."""

from __future__ import annotations

from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger
from pv_growth.jobs.runner import handler

log = get_logger("mirza.sync")


@handler("mirza.sync")
def _sync_job(session: Session, settings: Settings, payload: dict) -> None:
    from pv_growth.mirza_adapter.mysql_reader import sync_mirza

    stats = sync_mirza(session, settings)
    log.info("mirza.sync done", **{k: v for k, v in stats.items() if k != "error"})
