"""Admin API (token-auth): dashboard + management for campaigns, templates,
rules, sources, flags, partners, experiments, content."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from pv_growth.api.deps import require_admin
from pv_growth.core.config import get_settings
from pv_growth.core.flags import FLAG_KEYS, FlagService
from pv_growth.database.base import session_scope
from pv_growth.database.models import (
    Campaign,
    CompetitorSource,
    ConfigSource,
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
        recent_events = session.execute(
            select(Event).order_by(Event.id.desc()).limit(20)
        ).scalars().all()
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
                {"event_id": e.event_id, "type": e.event_type,
                 "user_id": e.user_id, "at": e.occurred_at.isoformat()}
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
        return [{"id": c.id, "code": c.code, "name": c.name, "kind": c.kind,
                 "status": c.status, "config": c.config} for c in rows]


@router.post("/campaigns", status_code=201)
def campaigns_create(payload: CampaignIn) -> dict:
    with session_scope(get_settings()) as session:
        clash = session.execute(
            select(Campaign).where(Campaign.code == payload.code)
        ).scalar_one_or_none()
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
        row = session.execute(
            select(Campaign).where(Campaign.code == code)
        ).scalar_one_or_none()
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
        return [{"code": t.code, "intent": t.intent, "body": t.body,
                 "active": bool(t.is_active)} for t in rows]


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
        return [{"code": r.code, "trigger": r.trigger, "active": bool(r.is_active),
                 "template": r.template_code, "cooldown_h": r.cooldown_hours,
                 "max_sends": r.max_sends, "stop": r.stop_conditions} for r in rows]


@router.post("/rules", status_code=201)
def rules_create(payload: RuleIn) -> dict:
    with session_scope(get_settings()) as session:
        session.add(LifecycleRule(**payload.model_dump()))
        return {"code": payload.code}


@router.patch("/rules/{code}")
def rules_toggle(code: str, payload: dict) -> dict:
    with session_scope(get_settings()) as session:
        row = session.execute(
            select(LifecycleRule).where(LifecycleRule.code == code)
        ).scalar_one_or_none()
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
        return [{"id": s.id, "kind": s.kind, "name": s.name, "url": s.url,
                 "active": s.is_active,
                 "reliability": round(s.ok_count / max(1, s.fetch_count), 3)}
                for s in rows]


@router.post("/sources", status_code=201)
def sources_create(payload: ConfigSourceIn) -> dict:
    with session_scope(get_settings()) as session:
        session.add(ConfigSource(kind=payload.kind, name=payload.name, url=payload.url))
        return {"name": payload.name}


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
            out.append({"key": exp.key, "status": exp.status,
                        "report": report})
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
        session.add(CompetitorSource(name=payload.name, url=payload.url,
                                     fields=payload.fields))
        return {"name": payload.name}
