# STATUS.md

Last updated: 2026-10-03

## Current Phase

Phase 0–5 — COMPLETE. Phase 6 is next.

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

- Phase 6 (Analytics / Admin / Experiments).

## Phase 5 summary

- Content engine: draft→validated→scheduled→published/failed state machine;
  commercial-fact validation against campaign records (contradictions, hype,
  fake user counts all rejected); idempotent publishing
- Feedback: 1–5 ratings idempotent per window; testimonial ONLY with stored
  consent; low ratings route to recovery workflow
- Competitor watch: admin-configured regex fields, structured change records
  (field/old/new/url/observed_at), deduped; failures fully isolated

## Phase 4 summary

- Referral codes + pending→valid→rewarded state machine; validation only on
  settled qualifying purchase; one referral row per referred user; daily
  anti-farming cap; self-referral blocked
- Reward ledger with unique dedupe_key (rewards can never pay twice);
  configurable milestones (1/3/5/10-style thresholds, data-driven)
- Partner records (affiliate/partner/reseller) with click/start counters,
  PARTNER_CONVERSION events, commission ledger (pending→approved, dedupe);
  reseller dashboard NOT rebuilt (spec §19)

## Phase 3 summary

- PostgreSQL-backed durable job queue (no Redis): atomic row-claim, exponential
  backoff retries, stale-lock recovery, idempotent enqueue; runner + background
  scheduler thread wired to app lifespan
- Messaging: strict template rendering (unknown/missing facts refuse to send —
  commercial facts only from structured data), effect log with unique dedupe_key,
  MESSAGE_SENT events
- Lifecycle rules (data-driven): triggers (started_no_trial, trial_no_connect,
  trial_no_purchase, checkout_abandoned, first_purchase_onboarding, winback),
  delay/conditions/cooldown/max_sends/stop_conditions — purchase kills sales reminders
- Segments: all 15 spec segments computed as pure functions of event history,
  recomputed every scan

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

- Phase 0–5 cumulative: 66 passed total (SQLite locally;
  CI additionally runs the same suite against PostgreSQL 14 + migration up/down).
- `ruff check src tests`: clean.
