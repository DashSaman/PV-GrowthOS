"""Targeted competitor watcher: admin-configured sources, regex-extracted
fields, structured diff records. Lightweight HTTP only; failures are fully
isolated (never affect VPN/Mirza/GrowthOS core)."""

from __future__ import annotations

import re

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.database.models import CompetitorChange, CompetitorSource
from pv_growth.database.types import utcnow

log = get_logger("competitors")


def extract_fields(text: str, spec: dict) -> dict:
    """Apply each field's regex to the page text; first capture group wins."""
    extracted: dict[str, str | None] = {}
    for field, config in (spec or {}).items():
        pattern = config.get("regex") if isinstance(config, dict) else None
        if not pattern:
            continue
        match = re.search(pattern, text, re.IGNORECASE)
        extracted[field] = match.group(1).strip() if match else None
    return extracted


def diff_snapshots(old: dict, new: dict) -> list[tuple[str, str | None, str | None]]:
    """[(field, old, new)] for changed or newly-seen fields."""
    changes = []
    for field, new_value in new.items():
        old_value = old.get(field)
        if new_value is not None and new_value != old_value:
            changes.append((field, old_value, new_value))
    return changes


def check_source(session: Session, source: CompetitorSource, client: httpx.Client) -> int:
    """Fetch one source, diff against last snapshot, persist change records."""
    source.last_checked_at = utcnow()
    try:
        resp = client.get(source.url, timeout=10.0,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; PVBot/1.0)"})
        resp.raise_for_status()
        snapshot = extract_fields(resp.text, source.fields)
    except httpx.HTTPError as exc:
        log.warning("competitor fetch failed (isolated)", source=source.name,
                    error=str(exc))
        return -1  # failure never propagates

    changes = diff_snapshots(source.last_snapshot or {}, snapshot)
    recorded = 0
    for field, old_value, new_value in changes:
        dedupe = f"cc:{source.id}:{field}:{new_value}:{(old_value or '')[:60]}"
        existing = session.execute(
            select(CompetitorChange).where(CompetitorChange.dedupe_key == dedupe)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        row = CompetitorChange(source_id=source.id, field=field, old_value=old_value,
                               new_value=new_value, dedupe_key=dedupe,
                               source_url=source.url)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
            recorded += 1
        except IntegrityError:
            continue
    if snapshot:
        source.last_snapshot = snapshot
    if recorded:
        log.info("competitor changes recorded", source=source.name, count=recorded)
    return recorded


def run_watch(session: Session, settings: Settings, flags: FlagService,
              client: httpx.Client | None = None) -> dict:
    """One watch cycle over all active sources. Flag-gated; failures isolated."""
    if not flags.enabled("COMPETITOR_WATCH_ENABLED"):
        return {"skipped": "disabled"}
    client = client or httpx.Client()
    sources = session.execute(
        select(CompetitorSource).where(CompetitorSource.is_active.is_(True))
    ).scalars().all()
    summary = {"sources": len(sources), "changes": 0, "failures": 0}
    for source in sources:
        result = check_source(session, source, client)
        if result < 0:
            summary["failures"] += 1
        else:
            summary["changes"] += result
    return summary
