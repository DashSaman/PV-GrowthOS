# STATUS.md

Last updated: 2026-10-03

## Current Phase

Phase 0 — COMPLETE. Phase 1 — COMPLETE. Phase 2 is next.

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

- Phase 2 (Free Acquisition Engine) implementation.

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

- Phase 0: 6 tests · Phase 1: +13 tests → 19 passed total (SQLite locally;
  CI additionally runs the same suite against PostgreSQL 14 + migration up/down).
- `ruff check src tests`: clean.
