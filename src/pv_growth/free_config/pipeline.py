"""Public-pool pipeline: fetch → parse → normalize → dedup → validate →
score → limited health check → rank → publish (idempotent).

Staged by design: thousands may be fetched, only a handful are ever
connection-tested, and only 1–2 published per slot. Publishing is guarded by
PublishedPost.dedupe_key so double scheduler runs cannot double-post.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

import httpx
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.acquisition.retail_offer import PURCHASE_URL
from pv_growth.config_quality.health import check_top_candidates
from pv_growth.config_quality.parsers import parse_any, score, validate
from pv_growth.config_sources.fetchers import fetch_source
from pv_growth.core.config import Settings
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.database.models import ConfigSource, PublishedPost, RawConfig
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.free_config.branding import brand_uri
from pv_growth.telegram.client import InlineKeyboard, TelegramClient

log = get_logger("pipeline")

PUBLIC_LABEL_FA = "🌐 منبع: عمومی/کامانیوتی (متعلق به PV Network نیست)"
PUBLIC_LABEL_EN = "community-sourced · not operated by PV Network"


def adopt_inactive_source(existing, parsed, source, active_source_ids):
    """Keep config identity, but require a new check under its fresh approved source."""
    if existing.source_id in active_source_ids:
        return False
    if not source.is_active or source.id not in active_source_ids or existing.uri_hash != parsed.uri_hash:
        return False
    existing.source_id = source.id
    existing.raw_uri = parsed.raw_uri
    existing.remark = parsed.remark
    existing.status = "validated"
    existing.health_checked = False
    existing.health_ok = None
    existing.fetched_at = utcnow()
    return True


def collect_and_stage(session: Session, client: httpx.Client, settings: Settings) -> dict:
    """Fetch all active sources, parse, dedup (unique uri_hash), validate, score."""
    stats = {"sources": 0, "fetched": 0, "new": 0, "duplicates": 0, "invalid": 0}
    seen_this_run: set[str] = set()
    sources = session.execute(select(ConfigSource).where(ConfigSource.is_active.is_(True))).scalars().all()
    active_source_ids = {source.id for source in sources}

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
                if adopt_inactive_source(existing, parsed, source, active_source_ids):
                    pass
                elif existing.status == "probe_failed":
                    existing.raw_uri = parsed.raw_uri
                    existing.status = "validated"
                    existing.fetched_at = utcnow()
                elif existing.status in {"validated", "published"}:
                    existing.fetched_at = utcnow()
                seen_this_run.add(parsed.uri_hash)
                stats["duplicates"] += 1
                continue
            q = score(parsed, source_reliability=reliability)
            quality_sum += q
            row = RawConfig(
                source_id=source.id,
                uri_hash=parsed.uri_hash,
                protocol=parsed.protocol,
                host=parsed.host,
                port=parsed.port,
                raw_uri=parsed.raw_uri,
                remark=parsed.remark,
                valid=True,
                quality_score=q,
                status="validated",
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


def rank_candidates(session: Session, settings: Settings, *, exclude_ids=()) -> list[RawConfig]:
    """Top fresh validated configs by score; health-check only these few."""
    active_sources = select(ConfigSource.id).where(ConfigSource.is_active.is_(True))
    health_priority = case(
        (RawConfig.health_ok.is_(True), 0),
        (RawConfig.health_checked.is_(False), 1),
        else_=2,
    )
    eligible = (
        select(
            RawConfig.id.label("config_id"),
            func.row_number()
            .over(
                partition_by=RawConfig.source_id,
                order_by=(health_priority, RawConfig.quality_score.desc(), RawConfig.id.asc()),
            )
            .label("source_rank"),
        )
        .where(
            RawConfig.valid.is_(True),
            RawConfig.id.not_in(exclude_ids),
            or_(
                RawConfig.status == "validated",
                (RawConfig.status == "published") & (RawConfig.published_at <= utcnow() - timedelta(hours=6)),
            ),
            RawConfig.fetched_at >= utcnow() - timedelta(hours=24),
            RawConfig.source_id.in_(active_sources) if settings.env == "production" else True,
        )
        .subquery()
    )
    candidates = (
        session.execute(
            select(RawConfig)
            .join(eligible, RawConfig.id == eligible.c.config_id)
            .order_by(
                health_priority,
                eligible.c.source_rank,
                RawConfig.quality_score.desc(),
                RawConfig.id.asc(),
            )
            .limit(
                min(settings.health_check_max, 6)
                if settings.env == "production" or settings.proxy_probe_required
                else settings.health_check_max
            )
        )
        .scalars()
        .all()
    )
    from pv_growth.free_config.autonomous import probe_allowance

    candidates = candidates[: probe_allowance(session, len(candidates))]
    results = check_top_candidates([(c.id, c.host, c.port) for c in candidates], settings)
    for candidate in candidates:
        candidate.health_checked = True
        candidate.health_ok = results.get(candidate.id)
        if candidate.health_ok and (settings.env == "production" or settings.proxy_probe_required):
            from pv_growth.config_quality.probe import probe_uri

            candidate.health_ok = probe_uri(candidate.raw_uri, settings).ok
        if not candidate.health_ok:
            candidate.status = "probe_failed"
    session.flush()
    ranked = [c for c in candidates if c.health_ok]
    ranked.sort(key=lambda c: c.quality_score, reverse=True)
    return ranked


def render_public_post(config: RawConfig, *, gated: bool = False, https_verified: bool = False) -> str:
    from pv_growth.free_config.sales_copy import config_footer

    if not gated:
        return (
            "🌐 <b>کانفیگ رایگان وب</b>\n\n"
            + (f"<code>{html.escape(config.remark or '')}</code>\n" if not https_verified else "")
            + ("✅ اتصال واقعی HTTPS پیش از انتشار از سرور ما بررسی شد.\n" if https_verified else "")
            + (
                "📋 کد زیر را کپی و در برنامهٔ اتصال وارد کن:\n\n"
                f"<code>{html.escape(brand_uri(config.raw_uri))}</code>\n\n"
                "ℹ️ منبع عمومی؛ متعلق به PV Network نیست. حجم، مدت و IP آن تحت کنترل ما نیست.\n"
                "نتیجهٔ اتصال روی اینترنت شما ممکن است متفاوت باشد.\n\n"
                f"{config_footer()}"
            )
        )
    remark = config.remark or f"{config.protocol} · {config.host}"
    return (
        f"🔓 <b>کانفیگ رایگان روز</b>\n"
        f"<code>{html.escape(remark)}</code>\n\n"
        f"پروتکل: <b>{config.protocol.upper()}</b>\n"
        f"{PUBLIC_LABEL_FA}\n{PUBLIC_LABEL_EN}\n\n"
        + (
            "📥 دریافت پس از عضویت در هر دو کانال، ۴۵ روز بدون خرید و نداشتن سرویس فعال.\n"
            "✅ اتصال واقعی HTTPS پیش از انتشار بررسی شد؛ نتیجه روی اینترنت شما ممکن است متفاوت باشد.\n\n"
            if gated
            else f"<code>{html.escape(config.raw_uri)}</code>\n\n"
        )
        + f"{config_footer()}"
    )


def publish_public_slot(
    session: Session,
    settings: Settings,
    flags: FlagService,
    telegram: TelegramClient,
    day: date | None = None,
    slot: int = 1,
    fulfillment_ceiling: int | None = None,
) -> PublishedPost | None:
    """Publish top configs for one day/slot. Idempotent on dedupe_key:
    a second call (crashed scheduler, double run) is a no-op."""
    if settings.env == "production":
        from pv_growth.free_config.community import enabled

        if not enabled(session, settings, flags):
            return None
    if not flags.enabled("FREE_CONFIG_ENABLED") or not flags.enabled("PUBLIC_CONFIG_ENABLED"):
        log.info("public config disabled by flag")
        return None
    from pv_growth.free_config.shared_public import FORBIDDEN_MAIN, growth_policy, local_day
    from pv_growth.free_config.web_publication import day_reservations, direct_enabled, lock_public_day

    direct = direct_enabled(session)
    if fulfillment_ceiling is not None and (
        type(fulfillment_ceiling) is not int or not 1 <= fulfillment_ceiling <= 10
    ):
        return None

    if (
        not settings.free_channel_id
        or str(settings.free_channel_id).lower() in FORBIDDEN_MAIN
        or not 1 <= slot <= (10 if direct else 2)
    ):
        return None
    day = day or (local_day(utcnow()) if direct else date.today())
    if direct and day != local_day(utcnow()):
        return None
    exclusions = []
    if direct:
        lock_public_day(session, day)
        if day != local_day(utcnow()):
            return None  # the lock wait may cross Tehran midnight
        reservations = day_reservations(session, settings, day)
        ceiling = min(10, growth_policy(session).get("community_daily_slots", 10), fulfillment_ceiling or 10)
        if len(reservations) >= ceiling:
            return None
        exclusions = [p.config_id for p in reservations if p.config_id is not None]
        from pv_growth.free_config.web_publication import config_alias_exclusions

        exclusions = config_alias_exclusions(session, exclusions)
    prefix = "free_public_direct" if direct else "free_public"
    dedupe_key = f"{prefix}:{day.isoformat()}:{slot}"
    existing = session.execute(
        select(PublishedPost).where(PublishedPost.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if existing is not None:
        return None  # already published this slot — never duplicate

    ranked = rank_candidates(session, settings, exclude_ids=exclusions)
    if direct:
        old_uris = {
            brand_uri(raw.raw_uri)
            for raw in session.scalars(select(RawConfig).where(RawConfig.id.in_(exclusions)))
        }
        ranked = [r for r in ranked if brand_uri(r.raw_uri) not in old_uris]
    if not ranked:
        log.info("no healthy candidates to publish", day=day.isoformat(), slot=slot)
        return None

    if settings.env == "production" or settings.proxy_probe_required:
        from pv_growth.config_quality.probe import probe_uri

        if not probe_uri(ranked[0].raw_uri, settings).ok:
            ranked[0].health_ok = False
            ranked[0].status = "probe_failed"
            return None

    reserved_at = utcnow()
    if direct and day != local_day(reserved_at):
        return None  # a connection probe may cross Tehran midnight
    post = PublishedPost(
        dedupe_key=dedupe_key,
        channel_id=settings.free_channel_id,
        kind="free_public",
        config_id=ranked[0].id,
        posted_at=reserved_at,
    )
    try:
        with session.begin_nested():
            session.add(post)
            session.flush()
    except IntegrityError:
        return None  # concurrent scheduler won the race

    session.commit()  # a remote result may be unknown; never roll back the effect reservation

    if direct and day != local_day(utcnow()):
        return None  # retain the reservation, but never send a stale-day post

    if settings.env == "production":
        from pv_growth.free_config.community import enabled

        source = session.get(ConfigSource, ranked[0].source_id, populate_existing=True)
        if (
            not enabled(session, settings, flags)
            or source is None
            or not source.is_active
            or (direct and not direct_enabled(session))
        ):
            return None

    from pv_growth.acquisition.retail_offer import guide_button

    result = telegram.send_channel_post(
        settings.free_channel_id,
        render_public_post(
            ranked[0],
            gated=settings.env == "production" and not direct,
            https_verified=settings.env == "production" or settings.proxy_probe_required,
        ),
        InlineKeyboard(
            [
                [
                    {
                        "text": "📥 دریافت کانفیگ عمومی وب",
                        "url": f"https://t.me/pvgrowthos_bot?start=freecfg_community_{ranked[0].id}",
                    }
                ],
                [{"text": "🛒 خرید سرویس PV Network", "url": PURCHASE_URL}],
                [guide_button("web")],
            ]
            if settings.env == "production" and not direct
            else [
                [{"text": "🛒 خرید سرویس PV Network", "url": PURCHASE_URL}],
                [guide_button("web")],
            ]
        ),
    )
    post.message_id = result.get("message_id")
    ranked[0].status = "published"
    ranked[0].published_at = utcnow()
    ingest(
        session,
        "FREE_CONFIG_POSTED",
        campaign_id=None,
        source_id=None,
        idempotency_key=f"freepost:{dedupe_key}",
        metadata={
            "dedupe_key": dedupe_key,
            "protocol": ranked[0].protocol,
            "slot": slot,
            "kind": "public",
            "health_method": "https_via_proxy"
            if settings.env == "production" or settings.proxy_probe_required
            else "tcp_dev_only",
            "checked_at_utc": utcnow().isoformat(),
        },
    )
    log.info("public config published", dedupe_key=dedupe_key, message_id=post.message_id)
    return post
