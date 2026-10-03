# STATUS.md

Last updated: 2026-10-03

## Current Phase

**FULLY OPERATIONAL — B5 CLOSED (2026-10-04, v1.0.2)**: dedicated bot @pvgrowthos_bot
LIVE (long-polling), channel t.me/pvnetwork_freeconfig LIVE, free-config
pipeline LIVE (1,846 real configs staged, post published), exclusive claim E2E
idempotent, referral A→B E2E with exactly-one reward, lifecycle canary sent,
Mirza 100% reconciled (7,272 pay / 213 checkout / 7,032 renewals / expiry
change-detection). 6/10 flags ON. Protected services verified unchanged.
B5 closed: real PV-exclusive provisioning reuses Mirza's own X-UI panel API
(XUIProvisioningAdapter, deterministic growth-* clients, fail-closed guard,
expiry sweep with panel evidence). Production E2E verified end-to-end:
claim → provision → panel-visible 1GB/24h service → subscription config URI →
duplicate idempotent → cleanup. DoD audit: 51 PASS · 0 FAIL · 0 BLOCKED
(including a live create+verify+cleanup probe on every audit run).

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

Nothing — system operational. Optional next: provisioning endpoint credential
(B5), then remaining 4 flags per rollout plan.

## Phase 7 summary (hardening)

- Placeholder/TODO/FIXME scan: clean (audit-grade, scripted)
- Machine-verifiable Definition-of-Done audit: scripts/dod_audit.py —
  42 PASS · 0 FAIL · 4 BLOCKED (SSH-only items)
- Missing spec modules campaigns/admin were added as real code (found by audit)
- Backup script (pg_dump + gzip verify + 14-copy rotation), load probe,
  resource limits enforced in compose (384M/0.75CPU/200pids)
- pip-audit in CI; migration up/down on both DB engines; rollback rehearsed in CI

## Phase 6 summary

- Analytics: funnel (6 steps), conversion rate, source performance (first-touch
  attribution → trials/purchases/revenue), monthly cohorts, revenue summary
  (total/renewal/winback), referral stats, campaign performance — all SQL over
  the event log, reconciling with raw counts (tested)
- Experiments: hash(seed,key,user) deterministic assignment, persisted per
  (experiment,user), immutable while running, results reported with
  insufficient-sample flag, never auto-declared winners
- Admin API (X-Admin-Token): dashboard (flags/funnel/revenue/queue/failures/
  recent events), flags toggle, campaigns/templates/rules/sources CRUD,
  partners summary, experiments create/list/end, competitor sources
- Telegram webhook: secret path; /start deep links → BOT_STARTED + attribution
  + referral/partner hooks; claim: and rate: callbacks; 503 without token

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

- Production deployed 2026-10-03: pv-growth-app @ 127.0.0.1:8350, alembic 0007,
  Mirza read-only sync LIVE (7,491 invoices scanned, real payment events),
  79 tests green, CI green. Deploy evidence: docs/deployment-audits/
- DoD audit final: see scripts/dod_audit.py output in the completion report
- **CI verified green on GitHub Actions (run #11, commit f046695):**
  lint + tests on SQLite AND PostgreSQL 14 matrix + migration up/down +
  pip-audit + docker build — all passing (SQLite locally;
  CI additionally runs the same suite against PostgreSQL 14 + migration up/down).
- `ruff check src tests`: clean.
