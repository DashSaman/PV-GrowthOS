# ARCHITECTURE.md

Modular monolith `pv-growth-app` (FastAPI + SQLAlchemy 2 + Alembic on the
existing PostgreSQL 14). One container; internal modules with clean seams so a
worker split later needs no redesign. No Redis, no heavy platforms in v1.

```
src/pv_growth/
├── main.py            FastAPI factory + lifespan (scheduler start/stop)
├── cli.py             python -m pv_growth: serve · migrate · preflight · smoke · scheduler · init-flags
├── core/              settings (env, PVG_ prefix) · JSON logging · feature flags · errors
├── database/          engine/session scopes · portable types (JSONB-on-PG) · models/ registry
├── api/               /health /ready · event ingestion · telegram webhook · admin API
├── preflight.py       read-only production preflight (subsystem of deploy contract)
├── smoke.py           protected-service smoke (before/after every deploy)
├── jobs/              PostgreSQL-backed durable queue: claim with row-locks, retries,
│                      idempotency keys; effects (messages/posts) deduped at effect level
├── events/            normalized event ingestion (idempotency_key unique)
├── attribution/       first-touch (immutable) + last-touch persistence
├── mirza_adapter/     read-only HTTP adapter to Mirza (source of truth)
├── telegram/          Bot API client (injectable transport) + deep links
├── config_sources/    approved public source registry + fetchers (GitHub raw, t.me/s preview)
├── config_quality/    vmess/vless/trojan/ss parsers · normalize · dedup hash · validate · score
├── free_config/       staged pipeline (fetch→…→publish top-N) + PV exclusive campaigns
├── campaigns/         campaign records + schedules
├── lifecycle/         rule engine (trigger/delay/conditions/cooldown/max/stop)
├── messaging/         templates with strict variable substitution + intent sets
├── referrals/         codes, validation-after-settlement, reward ledger, anti-fraud
├── partners/          affiliate/partner/reseller records + commission ledger
├── content/           content state machine (draft→validated→scheduled→published/failed)
├── competitors/       targeted watcher → structured change records
├── feedback/          1–5 ratings + testimonial consent
├── analytics/         SQL funnels, cohorts, source/revenue attribution
├── experiments/       deterministic A/B assignment (persisted, immutable per run)
├── segments/ scoring/ | recalculable segments; configurable lead scoring
└── admin/             token-auth management + dashboard API
```

## Key decisions
- **Jobs on Postgres, not Redis**: single durable queue table; claim via
  atomic row UPDATE (locked_at/locked_by) — works on PG and SQLite alike;
  duplicate scheduler instances cannot double-run a job; effects additionally
  deduped by unique keys (message_log, published_post).
- **Subnet decision**: 172.23.77.0/24 preferred; preflight verifies against
  live routes/docker networks and documents any alternative choice here.
  (Verification pending server access — BLOCKERS.md B1.)
- **Tests**: same suite on SQLite (dev) and PostgreSQL 14 (CI service) —
  JSON columns degrade via JSON variant; row-claim semantics identical.
- **Feature flags**: env default OFF; DB override; 15 s cache. Customer-facing
  automations stay OFF until their phase acceptance passes.

See docs/images/architecture.png for the visual overview.
