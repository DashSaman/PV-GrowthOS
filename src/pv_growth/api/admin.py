"""Admin API (token-auth): dashboard + management for campaigns, templates,
rules, sources, flags, partners, experiments, content."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from pv_growth.api.deps import require_admin
from pv_growth.content import service as content_service
from pv_growth.content.optimizer import rank_candidates
from pv_growth.content.planner import plan_cycle
from pv_growth.core.config import get_settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FLAG_KEYS, FlagService
from pv_growth.database.base import session_scope
from pv_growth.database.models import (
    Campaign,
    CompetitorSource,
    ConfigSource,
    ContentInsight,
    ContentItem,
    ContentPublication,
    Event,
    Experiment,
    LifecycleRule,
    MessageTemplate,
    Partner,
)

router = APIRouter(dependencies=[Depends(require_admin)])


# ---------- dashboard ----------


@router.get("/dashboard")
def dashboard() -> dict:
    from pv_growth.analytics import queries

    settings = get_settings()
    with session_scope(settings) as session:
        recent_events = session.execute(select(Event).order_by(Event.id.desc()).limit(20)).scalars().all()
        from pv_growth.admin.service import system_health

        health = system_health(session)
        return {
            "flags": FlagService(settings).snapshot(),
            "funnel": queries.funnel(session),
            "conversion_rate": queries.conversion_rate(session),
            "revenue": queries.revenue_summary(session),
            "referrals": queries.referral_stats(session),
            "sources": queries.source_performance(session),
            "campaigns": queries.campaign_performance(session),
            "system": health,
            "recent_events": [
                {
                    "event_id": e.event_id,
                    "type": e.event_type,
                    "user_id": e.user_id,
                    "at": e.occurred_at.isoformat(),
                }
                for e in recent_events
            ],
        }


@router.get("/analytics/cohorts")
def cohorts() -> list[dict]:
    from pv_growth.analytics import queries

    with session_scope(get_settings()) as session:
        return queries.cohorts(session)


# ---------- feature flags ----------


@router.get("/flags")
def list_flags() -> dict:
    return FlagService(get_settings()).snapshot()


@router.put("/flags/{key}")
def set_flag(key: str, payload: dict) -> dict:
    from pv_growth.database.models import FeatureFlag

    if key not in FLAG_KEYS:
        from fastapi import HTTPException

        raise HTTPException(422, f"unknown flag: {key}")
    with session_scope(get_settings()) as session:
        row = session.get(FeatureFlag, key)
        if row is None:
            row = FeatureFlag(key=key, enabled=bool(payload.get("enabled")))
            session.add(row)
        else:
            row.enabled = bool(payload.get("enabled"))
        session.flush()
        return {"key": key, "enabled": row.enabled}


# ---------- campaigns / templates / rules / sources ----------


class CampaignIn(BaseModel):
    code: str = Field(min_length=2, max_length=64)
    name: str
    kind: str
    status: str = "draft"
    config: dict = {}


@router.get("/campaigns")
def campaigns_list() -> list[dict]:
    with session_scope(get_settings()) as session:
        rows = session.execute(select(Campaign)).scalars().all()
        return [
            {
                "id": c.id,
                "code": c.code,
                "name": c.name,
                "kind": c.kind,
                "status": c.status,
                "config": c.config,
            }
            for c in rows
        ]


@router.post("/campaigns", status_code=201)
def campaigns_create(payload: CampaignIn) -> dict:
    with session_scope(get_settings()) as session:
        clash = session.execute(select(Campaign).where(Campaign.code == payload.code)).scalar_one_or_none()
        if clash is not None:
            from fastapi import HTTPException

            raise HTTPException(409, "campaign code exists")
        row = Campaign(**payload.model_dump())
        session.add(row)
        session.flush()
        return {"id": row.id, "code": row.code}


@router.patch("/campaigns/{code}")
def campaigns_update(code: str, payload: dict) -> dict:
    with session_scope(get_settings()) as session:
        row = session.execute(select(Campaign).where(Campaign.code == code)).scalar_one_or_none()
        if row is None:
            from fastapi import HTTPException

            raise HTTPException(404)
        for field in ("name", "status", "config", "kind"):
            if field in payload:
                setattr(row, field, payload[field])
        return {"code": code, "status": row.status}


class TemplateIn(BaseModel):
    code: str
    intent: str
    body: str
    cta_label: str | None = None
    cta_url: str | None = None


@router.get("/templates")
def templates_list() -> list[dict]:
    with session_scope(get_settings()) as session:
        rows = session.execute(select(MessageTemplate)).scalars().all()
        return [
            {"code": t.code, "intent": t.intent, "body": t.body, "active": bool(t.is_active)} for t in rows
        ]


@router.post("/templates", status_code=201)
def templates_create(payload: TemplateIn) -> dict:
    with session_scope(get_settings()) as session:
        session.add(MessageTemplate(**payload.model_dump()))
        return {"code": payload.code}


class RuleIn(BaseModel):
    code: str
    trigger: str
    template_code: str
    delay_minutes: int = 0
    conditions: dict = {}
    cooldown_hours: int = 24
    max_sends: int = 1
    stop_conditions: list = []


@router.get("/rules")
def rules_list() -> list[dict]:
    with session_scope(get_settings()) as session:
        rows = session.execute(select(LifecycleRule)).scalars().all()
        return [
            {
                "code": r.code,
                "trigger": r.trigger,
                "active": bool(r.is_active),
                "template": r.template_code,
                "cooldown_h": r.cooldown_hours,
                "max_sends": r.max_sends,
                "stop": r.stop_conditions,
            }
            for r in rows
        ]


@router.post("/rules", status_code=201)
def rules_create(payload: RuleIn) -> dict:
    with session_scope(get_settings()) as session:
        session.add(LifecycleRule(**payload.model_dump()))
        return {"code": payload.code}


@router.patch("/rules/{code}")
def rules_toggle(code: str, payload: dict) -> dict:
    with session_scope(get_settings()) as session:
        row = session.execute(select(LifecycleRule).where(LifecycleRule.code == code)).scalar_one_or_none()
        if row is None:
            from fastapi import HTTPException

            raise HTTPException(404)
        row.is_active = 1 if payload.get("active") else 0
        return {"code": code, "active": bool(row.is_active)}


class ConfigSourceIn(BaseModel):
    kind: str
    name: str
    url: str


@router.get("/sources")
def sources_list() -> list[dict]:
    with session_scope(get_settings()) as session:
        rows = session.execute(select(ConfigSource)).scalars().all()
        return [
            {
                "id": s.id,
                "kind": s.kind,
                "name": s.name,
                "url": s.url,
                "active": s.is_active,
                "reliability": round(s.ok_count / max(1, s.fetch_count), 3),
            }
            for s in rows
        ]


@router.post("/sources", status_code=201)
def sources_create(payload: ConfigSourceIn) -> dict:
    with session_scope(get_settings()) as session:
        session.add(ConfigSource(kind=payload.kind, name=payload.name, url=payload.url))
        return {"name": payload.name}


# ---------- content ----------


class ContentIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    kind: str = Field(default="education", min_length=1, max_length=32)
    channel: str = Field(default="free", min_length=1, max_length=32)
    format: str = Field(default="text", min_length=1, max_length=16)
    body: str = Field(min_length=1)
    facts: dict = Field(default_factory=dict)
    creative: dict = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, max_length=128)
    campaign_code: str | None = Field(default=None, max_length=64)


class ContentScheduleIn(BaseModel):
    when: datetime


def _content_out(item: ContentItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "kind": item.kind,
        "channel": item.channel,
        "format": item.format,
        "body": item.body,
        "facts": item.facts,
        "creative": item.creative,
        "status": item.status,
        "dedupe_key": item.dedupe_key,
        "campaign_code": item.campaign_code,
        "scheduled_at": item.scheduled_at.isoformat() if item.scheduled_at else None,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "message_id": item.message_id,
    }


def _require_content(session, item_id: int) -> ContentItem:
    item = session.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "content item not found")
    return item


@router.get("/content")
def content_list() -> list[dict]:
    with session_scope(get_settings()) as session:
        rows = session.execute(select(ContentItem).order_by(ContentItem.id.desc()).limit(200)).scalars()
        return [_content_out(item) for item in rows]


@router.post("/content", status_code=201)
def content_create(payload: ContentIn) -> dict:
    with session_scope(get_settings()) as session:
        item = ContentItem(**payload.model_dump())
        session.add(item)
        session.flush()
        return _content_out(item)


@router.post("/content/{item_id}/validate")
def content_validate(item_id: int) -> dict:
    with session_scope(get_settings()) as session:
        item = _require_content(session, item_id)
        try:
            content_service.transition(session, item, "validated")
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _content_out(item)


@router.post("/content/{item_id}/schedule")
def content_schedule(item_id: int, payload: ContentScheduleIn) -> dict:
    with session_scope(get_settings()) as session:
        item = _require_content(session, item_id)
        try:
            content_service.schedule(session, item, payload.when)
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _content_out(item)


@router.post("/content/{item_id}/retry")
def content_retry(item_id: int) -> dict:
    with session_scope(get_settings()) as session:
        item = _require_content(session, item_id)
        try:
            content_service.transition(session, item, "draft")
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _content_out(item)


# ---------- Instagram growth loop ----------


@router.get("/instagram/readiness")
def instagram_readiness() -> dict:
    settings = get_settings()
    missing = []
    required = {
        "instagram_account_id": settings.instagram_account_id,
        "instagram_access_token": settings.instagram_access_token,
        "instagram_api_version": settings.instagram_api_version,
        "media_public_base_url": settings.media_public_base_url,
        "media_store_root": settings.media_store_root,
    }
    for name, value in required.items():
        if not value:
            missing.append(name)
    flags = FlagService(settings).snapshot()
    api_configured = all(
        required[name] for name in ("instagram_account_id", "instagram_access_token", "instagram_api_version")
    )
    media_configured = bool(required["media_public_base_url"] and required["media_store_root"])
    return {
        "api_credentials_configured": api_configured,
        "public_media_store_configured": media_configured,
        "content_engine_enabled": flags["CONTENT_ENGINE_ENABLED"],
        "instagram_automation_enabled": flags["INSTAGRAM_AUTOMATION_ENABLED"],
        "ready_for_canary": api_configured and media_configured,
        "missing": missing,
    }


@router.get("/instagram/publications")
def instagram_publications() -> list[dict]:
    with session_scope(get_settings()) as session:
        rows = (
            session.execute(
                select(ContentPublication)
                .where(ContentPublication.provider == "instagram")
                .order_by(ContentPublication.id.desc())
                .limit(200)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "content_id": row.content_id,
                "status": row.status,
                "container_id": row.container_id,
                "media_id": row.media_id,
                "error_category": row.error_category,
                "attempt": row.attempt,
                "published_at": row.published_at.isoformat() if row.published_at else None,
            }
            for row in rows
        ]


@router.get("/instagram/insights")
def instagram_insights() -> list[dict]:
    with session_scope(get_settings()) as session:
        rows = (
            session.execute(select(ContentInsight).order_by(ContentInsight.id.desc()).limit(500))
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "publication_id": row.publication_id,
                "captured_at": row.captured_at.isoformat(),
                "metrics": row.metrics,
            }
            for row in rows
        ]


@router.post("/instagram/plan/preview")
def instagram_plan_preview() -> list[dict]:
    """Run real fact validation/planning inside a rolled-back savepoint."""
    settings = get_settings()
    with session_scope(settings) as session:
        savepoint = session.begin_nested()
        try:
            planned = plan_cycle(session, settings)
            preview = [_content_out(item) for item in planned]
        finally:
            savepoint.rollback()
        return preview


@router.get("/instagram/rankings")
def instagram_rankings() -> list[dict]:
    with session_scope(get_settings()) as session:
        content_ids = list(
            session.execute(
                select(ContentItem.id).where(
                    ContentItem.channel == "instagram", ContentItem.status == "published"
                )
            )
            .scalars()
            .all()
        )
        return [asdict(score) for score in rank_candidates(session, content_ids)]


# ---------- partners / experiments / competitor sources ----------


@router.get("/partners")
def partners_list() -> list[dict]:
    from pv_growth.partners import service as partners_svc

    with session_scope(get_settings()) as session:
        rows = session.execute(select(Partner)).scalars().all()
        return [partners_svc.partner_summary(session, p) for p in rows]


class ExperimentIn(BaseModel):
    key: str
    name: str
    variants: list[str]
    split_percent: int = 50
    metric_event: str = "PAYMENT_SUCCESS"


@router.get("/experiments")
def experiments_list() -> list[dict]:
    from pv_growth.experiments import service as exp_svc

    with session_scope(get_settings()) as session:
        rows = session.execute(select(Experiment)).scalars().all()
        out = []
        for exp in rows:
            report = exp_svc.results(session, exp.key)
            out.append({"key": exp.key, "status": exp.status, "report": report})
        return out


@router.post("/experiments", status_code=201)
def experiments_create(payload: ExperimentIn) -> dict:
    from pv_growth.experiments import service as exp_svc

    with session_scope(get_settings()) as session:
        exp = exp_svc.create_experiment(session, **payload.model_dump())
        return {"key": exp.key, "seed": exp.seed}


@router.post("/experiments/{key}/end")
def experiments_end(key: str) -> dict:
    from pv_growth.experiments import service as exp_svc

    with session_scope(get_settings()) as session:
        exp_svc.end_experiment(session, key)
        return {"key": key, "status": "ended"}


class CompetitorSourceIn(BaseModel):
    name: str
    url: str
    fields: dict


@router.post("/competitor-sources", status_code=201)
def competitor_sources_create(payload: CompetitorSourceIn) -> dict:
    with session_scope(get_settings()) as session:
        session.add(CompetitorSource(name=payload.name, url=payload.url, fields=payload.fields))
        return {"name": payload.name}
