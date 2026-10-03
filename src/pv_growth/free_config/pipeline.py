"""Public-pool pipeline: fetch → parse → normalize → dedup → validate →
score → limited health check → rank → publish (idempotent).

Staged by design: thousands may be fetched, only a handful are ever
connection-tested, and only 1–2 published per slot. Publishing is guarded by
PublishedPost.dedupe_key so double scheduler runs cannot double-post.
"""

from __future__ import annotations

from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.config_quality.health import check_top_candidates
from pv_growth.config_quality.parsers import parse_any, score, validate
from pv_growth.config_sources.fetchers import fetch_source
from pv_growth.core.config import Settings
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.database.models import ConfigSource, PublishedPost, RawConfig
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.telegram.client import TelegramClient

log = get_logger("pipeline")

PUBLIC_LABEL_FA = "🌐 منبع: عمومی/کامانیوتی (متعلق به PV Network نیست)"
PUBLIC_LABEL_EN = "community-sourced · not operated by PV Network"


def collect_and_stage(session: Session, client: httpx.Client, settings: Settings) -> dict:
    """Fetch all active sources, parse, dedup (unique uri_hash), validate, score."""
    stats = {"sources": 0, "fetched": 0, "new": 0, "duplicates": 0, "invalid": 0}
    seen_this_run: set[str] = set()
    sources = session.execute(
        select(ConfigSource).where(ConfigSource.is_active.is_(True))
    ).scalars().all()

    for source in sources:
        stats["sources"] += 1
        source.fetch_count += 1
        source.last_fetched_at = utcnow()
        try:
            uris = fetch_source(source.kind, source.url, client)[: settings.fetch_max_configs]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("source fetch failed", source=source.name, error=str(exc))
            continue

        source.ok_count += 1
        source.last_success_at = utcnow()
        quality_sum = 0.0
        reliability = source.ok_count / max(1, source.fetch_count)
        for uri in uris:
            stats["fetched"] += 1
            parsed = parse_any(uri)
            if parsed is None:
                stats["invalid"] += 1
                continue
            reason = validate(parsed)
            if reason is not None:
                stats["invalid"] += 1
                continue
            if parsed.uri_hash in seen_this_run:
                stats["duplicates"] += 1
                continue
            existing = session.execute(
                select(RawConfig).where(RawConfig.uri_hash == parsed.uri_hash)
            ).scalar_one_or_none()
            if existing is not None:
                seen_this_run.add(parsed.uri_hash)
                stats["duplicates"] += 1
                continue
            q = score(parsed, source_reliability=reliability)
            quality_sum += q
            row = RawConfig(
                source_id=source.id, uri_hash=parsed.uri_hash, protocol=parsed.protocol,
                host=parsed.host, port=parsed.port, raw_uri=parsed.raw_uri,
                remark=parsed.remark, valid=True, quality_score=q, status="validated",
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                stats["duplicates"] += 1
                continue
            seen_this_run.add(parsed.uri_hash)
            stats["new"] += 1
        if stats["new"] or quality_sum:
            source.avg_quality = round(quality_sum / max(1, len(uris)), 4)
    log.info("collect staged", **stats)
    return stats


def rank_candidates(session: Session, settings: Settings) -> list[RawConfig]:
    """Top fresh validated configs by score; health-check only these few."""
    candidates = session.execute(
        select(RawConfig)
        .where(RawConfig.valid.is_(True), RawConfig.status == "validated")
        .order_by(RawConfig.quality_score.desc())
        .limit(settings.health_check_max)
    ).scalars().all()
    results = check_top_candidates(
        [(c.id, c.host, c.port) for c in candidates], settings
    )
    for candidate in candidates:
        candidate.health_checked = True
        candidate.health_ok = results.get(candidate.id)
    session.flush()
    ranked = [c for c in candidates if c.health_ok]
    ranked.sort(key=lambda c: c.quality_score, reverse=True)
    return ranked


def render_public_post(config: RawConfig) -> str:
    remark = config.remark or f"{config.protocol} · {config.host}"
    return (
        f"🔓 <b>کانفیگ رایگان روز</b>\n"
        f"<code>{remark}</code>\n\n"
        f"پروتکل: <b>{config.protocol.upper()}</b>\n"
        f"{PUBLIC_LABEL_FA}\n{PUBLIC_LABEL_EN}"
    )


def publish_public_slot(
    session: Session,
    settings: Settings,
    flags: FlagService,
    telegram: TelegramClient,
    day: date | None = None,
    slot: int = 1,
) -> PublishedPost | None:
    """Publish top configs for one day/slot. Idempotent on dedupe_key:
    a second call (crashed scheduler, double run) is a no-op."""
    if not flags.enabled("FREE_CONFIG_ENABLED") or not flags.enabled("PUBLIC_CONFIG_ENABLED"):
        log.info("public config disabled by flag")
        return None
    day = day or date.today()
    dedupe_key = f"free_public:{day.isoformat()}:{slot}"
    existing = session.execute(
        select(PublishedPost).where(PublishedPost.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if existing is not None:
        return None  # already published this slot — never duplicate

    ranked = rank_candidates(session, settings)
    if not ranked:
        log.info("no healthy candidates to publish", day=day.isoformat(), slot=slot)
        return None

    post = PublishedPost(
        dedupe_key=dedupe_key, channel_id=settings.free_channel_id,
        kind="free_public", config_id=ranked[0].id,
    )
    try:
        with session.begin_nested():
            session.add(post)
            session.flush()
    except IntegrityError:
        return None  # concurrent scheduler won the race

    result = telegram.send_channel_post(
        settings.free_channel_id, render_public_post(ranked[0])
    )
    post.message_id = result.get("message_id")
    ranked[0].status = "published"
    ranked[0].published_at = utcnow()
    ingest(
        session, "FREE_CONFIG_POSTED",
        campaign_id=None, source_id=None,
        idempotency_key=f"freepost:{dedupe_key}",
        metadata={"dedupe_key": dedupe_key, "protocol": ranked[0].protocol,
                  "slot": slot, "kind": "public"},
    )
    log.info("public config published", dedupe_key=dedupe_key,
             message_id=post.message_id)
    return post
