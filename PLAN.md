# PLAN.md — Single Execution Plan

The only execution plan. Phase order is fixed; exactly one phase is ACTIVE.
Phase gates require every applicable acceptance item to pass before the next
phase starts. New ideas go to `BACKLOG.md` and never interrupt the active phase.

## Phase Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundation & Safety | ✅ COMPLETE |
| 1 | Core Data / Events / Attribution / Mirza Adapter | ⬜ PENDING |
| 2 | Free Acquisition Engine | ⬜ PENDING |
| 3 | Lifecycle Sales Automation | ⬜ PENDING |
| 4 | Referral / Viral / Partner | ⬜ PENDING |
| 5 | Content / Feedback / Competitor Intel | ⬜ PENDING |
| 6 | Analytics / Admin / Experiments | ⬜ PENDING |
| 7 | Production Hardening & Rollout | ⬜ PENDING |

## Phase 0 — Foundation & Safety (COMPLETE)

Goal: runnable skeleton with safety tooling; zero production changes.

Scope & files:
- `src/pv_growth/` — FastAPI bootstrap, `/health`, `/ready`, settings, JSON logging, feature-flag service, DB engine/session layer
- `alembic/` — migration environment + `0001_bootstrap` (feature_flag table)
- `src/pv_growth/preflight.py` — read-only production preflight (protected paths/containers/ports/subnet/disk/mem/PG)
- `src/pv_growth/smoke.py` — protected-service smoke checks (Mirza, reseller, AKH, Apache, X-UI, PG, GrowthOS)
- `scripts/sentinelx-cleanup.sh` — inspect-save + container-only removal (server-side)
- `Dockerfile`, `docker-compose.yml` (limits: 384M / 0.75 CPU / pids 200, bind 127.0.0.1:8350)
- `.github/workflows/ci.yml` — lint, tests (sqlite+postgres matrix), migration up/down, pip-audit, docker build
- `tests/` — health/ready/preflight unit tests

Acceptance (verified):
- [x] source application exists (`pv-growth` package, installable)
- [x] FastAPI starts (`python -m pv_growth serve`)
- [x] `/health` works (test_health.py)
- [x] `/ready` works, 503 when DB down (test_health.py)
- [x] tests run and pass (6 passed)
- [x] migrations work in test env (conftest runs `alembic upgrade head`)
- [x] Docker image buildable (CI job; resource-light, never on prod server)
- [x] preflight exists, read-only, unit-tested (port parse, subnet overlap)
- [x] protected-resource smoke tests exist
- [x] no protected resource changed (development machine only; server actions BLOCKED — see BLOCKERS.md)
- [ ] sentinelx-worker cleanup executed on server — BLOCKED (no server access from dev machine; script ready)
- [x] rollback steps documented (ROLLBACK.md)

## Phase 1 — Core Data / Events / Attribution / Mirza Adapter

Goal: schema + event ingestion with idempotency + first/last-touch attribution + safe Mirza adapter.

Planned files: `database/models/{users,sources,campaigns,events}.py`, `events/service.py`,
`attribution/service.py`, `mirza_adapter/client.py`, `api/events.py` (ingestion endpoint),
migration `0002_phase1`, tests: idempotent ingestion, first-touch immutability, Mirza fake.

Acceptance:
- [ ] bot/user event can be ingested
- [ ] source persists
- [ ] first-touch persists and is never overwritten
- [ ] duplicate events are idempotent (unique idempotency key)
- [ ] no payment/provisioning regression (adapter is read-only; Mirza untouched)

## Phase 2 — Free Acquisition Engine

Source registry, public collectors (GitHub/Telegram), parsers, dedup, quality scoring,
limited health validation, scheduler, free-channel publishing, PV exclusive campaign
model with admin-configurable per-date overrides.

Acceptance:
- [ ] two scheduled posts cannot duplicate
- [ ] invalid configs do not publish
- [ ] PV exclusive quota/date settings work
- [ ] source attribution survives the deep-link flow

## Phase 3 — Lifecycle Sales Automation

Segments, Postgres-backed durable job engine, message templates, trial follow-up,
checkout recovery, renewal reminders, expiry, win-back.

Acceptance:
- [ ] messages respect stop conditions (purchase stops sales reminders)
- [ ] idempotency prevents duplicate sends
- [ ] cooldown works

## Phase 4 — Referral / Viral / Partner

Referral codes, validation-after-settled-purchase, reward ledger, anti-abuse,
milestones, affiliate records, partner conversion, commission ledger.

Acceptance:
- [ ] self-referral blocked
- [ ] duplicate rewards impossible
- [ ] rewards only after qualifying conversion

## Phase 5 — Content / Feedback / Competitor Intelligence

Official/free channel scheduling, content state machine (draft→validated→scheduled→published/failed),
commercial-fact validation, feedback/rating with testimonial consent, targeted competitor watcher.

Acceptance:
- [ ] commercial facts cannot be fabricated
- [ ] scheduler idempotent
- [ ] competitor failures never affect VPN/Mirza

## Phase 6 — Analytics / Admin / Experiments

Admin interface (auth + management endpoints + dashboard), funnel, source analytics,
revenue attribution, renewals, cohorts, lead scoring, deterministic A/B assignment.

Acceptance:
- [ ] admin can trace source → trial → purchase → renewal
- [ ] analytics reconcile against underlying events/orders

## Phase 7 — Production Hardening & Rollout

Resource profiling, security review, dependency scan, load test, migration verification,
backup verification, rollback rehearsal, feature-flag rollout (gradual, never all-at-once).

## Rules

- Inspect → implement → test → document → commit (small, meaningful) → push.
- Anti-loop: after 3 failed serious attempts, document and change approach.
- Never declare complete without the machine-verifiable Definition of Done audit.
