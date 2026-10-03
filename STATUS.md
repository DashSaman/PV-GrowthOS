# STATUS.md

Last updated: 2026-10-03

## Current Phase

Phase 0/1/2 — COMPLETE. Phase 3 is next.

## Environment

Development is happening on a workstation (Windows/Git Bash, Python 3.12).
The production Ubuntu 22.04 server is **not reachable** from this machine —
all server-side actions (preflight run, sentinelx removal, deployment) are
recorded as blockers with ready-to-run scripts. All code, tests, CI and docs
are being built here and pushed to GitHub.

## Done

- Phase 0: FastAPI app skeleton (`/health`, `/ready`), settings via env,
  JSON structured logging, feature-flag service (DB override + env force),
  SQLAlchemy 2 + Alembic bootstrap migration, read-only preflight CLI,
  protected-service smoke CLI, sentinelx cleanup script (server-side),
  Dockerfile + compose with resource limits, GitHub Actions CI
  (lint/test-matrix sqlite+postgres/migrations/pip-audit/docker build),
  6 unit tests passing, ruff clean.

## In Progress

- Phase 3 (Lifecycle Sales Automation + durable job engine).

## Phase 2 summary

- config_sources registry + fetchers (GitHub raw, t.me/s public preview — lightweight HTTP only)
- Parsers vmess/vless/trojan/ss (SIP002): normalize, credential-fingerprint dedup hash,
  validation (private hosts, ports, credentials), bounded quality scoring
- Limited health check: TCP-only, hard caps (5 candidates, concurrency 3) — no mass testing
- Publish pipeline idempotent per day+slot (dedupe_key + savepoint); unhealthy never publish;
  public posts always labeled community-sourced
- PV exclusive pool: per-date overrides, quotas, per-user limits, idempotent claims,
  provisioning interface + fake; provisioning outage queues claim (never loses it)

## Phase 1 summary

- Schema: users/campaigns/sources/events/attribution_touch (migration 0002, tested up/down in CI)
- Event service: 23 normalized types, unique idempotency_key with savepoint-based
  concurrent-safe dedupe; SOURCE_ATTRIBUTED idempotent per user+source
- Attribution: start= parser (freecfg/ref/partner/seo/channel/social/custom/direct),
  first-touch immutable, last-touch upsert
- Mirza adapter: GET-only HTTP client + FakeMirza; NotConfigured degradation
- Telegram client: injectable transport (Http/Fake), webhook-safe
- API: POST/GET /api/events (admin token + rate limit), 201/created=false on duplicates

## Blockers

See `BLOCKERS.md` — all are external/credential-only; none freeze development.

## Test Results

- Phase 0: 6 · Phase 1: +13 · Phase 2: +15 → 34 passed total (SQLite locally;
  CI additionally runs the same suite against PostgreSQL 14 + migration up/down).
- `ruff check src tests`: clean.
