# STATUS.md

Last updated: 2026-10-03

## Current Phase

Phase 0 — COMPLETE. Phase 1 is next (not yet started).

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

- Phase 1 kick-off (schema, events, attribution, Mirza adapter).

## Blockers

See `BLOCKERS.md` — all are external/credential-only; none freeze development.

## Test Results (Phase 0)

- `pytest`: 6 passed (health/ready/preflight-logic) on SQLite; CI additionally
  runs the same suite against PostgreSQL 14 service + migration up/down test.
- `ruff check src tests`: clean.
