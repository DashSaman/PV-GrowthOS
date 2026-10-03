# Changelog

All notable changes to PV GrowthOS. Format loosely follows Keep a Changelog.

## [1.0.0] — 2026-10-03 — Phase 7: Hardening + Definition of Done

### Added
- scripts/dod_audit.py: machine-verifiable completion audit — 42 PASS / 0 FAIL / 4 BLOCKED (external-only)
- scripts/backup.sh (pv_growth-only pg_dump + verification + rotation), scripts/loadtest.py (modest probe)
- campaigns/ and admin/ promoted to real modules (found by the placeholder audit)
- README updated to implemented state (EN/FA) with quickstart + audit instructions

### Security
- pip-audit job in CI on every push; secrets remain env-only; admin token required

## [0.7.0] — 2026-10-03 — Phase 6: Analytics / Admin / Experiments

### Added
- Migration 0007: experiments, experiment_assignments (unique per user), app_configs
- Analytics: funnels, conversion, source performance (first-touch), cohorts, revenue/renewal/winback summaries, campaign performance; reconciliation tested
- Experiment engine: deterministic hash assignment, persisted & immutable per running experiment, no auto-declared winners on weak samples
- Admin API: authed dashboard + management endpoints (campaigns/templates/rules/sources/flags/partners/experiments/competitor sources)
- Telegram webhook with secret path, deep-link attribution + referral/partner hooks, claim/rating callbacks (503 when unconfigured)
- 7 new tests (73 total passing)

## [0.6.0] — 2026-10-03 — Phase 5: Content / Feedback / Competitor Intel

### Added
- Migration 0006: content_items, feedback_ratings, competitor_sources, competitor_changes (unique dedupe)
- Content engine: state machine + commercial-fact validation (fabrication/hype/fake-counts rejected); idempotent channel publishing
- Feedback engine: windowed idempotent ratings, consent-gated testimonials, recovery routing
- Competitor watcher: configured regex extraction, structured diff records, fully isolated failures
- 10 new tests (66 total passing)

## [0.5.0] — 2026-10-03 — Phase 4: Referral / Viral / Partner

### Added
- Migration 0005: referral_codes, referrals (unique referred_user), reward_ledger (unique dedupe_key), milestones, partners, commission_entries (unique dedupe_key)
- Referral engine: pending→valid(settled order)→rewarded; self-referral blocked; per-referred-user uniqueness; daily farming cap; bot-start hook (start=ref_<code>)
- Reward ledger: single pay point, idempotent forever; configurable milestones granted exactly once
- Partner engine: affiliate/partner/reseller records, click/start tracking (start=partner_<code>), PARTNER_CONVERSION, commission pending→approved with dedupe; reseller dashboard untouched
- 9 new tests (56 total passing)

## [0.4.0] — 2026-10-03 — Phase 3: Lifecycle Sales Automation

### Added
- Migration 0004: jobs (unique idempotency_key, due-index), message_templates, message_log (unique dedupe_key), lifecycle_rules
- Durable job engine: atomic claim (single-row UPDATE), exponential backoff (60s→1h cap), stale-lock recovery, dead-end after max attempts; handler registry; foreground runner + background scheduler thread
- Messaging engine: strict {{var}} rendering (missing/unknown facts refuse send), effect log, MESSAGE_SENT emission, idempotent sends
- Lifecycle rule engine: data-driven triggers/delay/conditions/cooldown/max_sends/stop_conditions; purchase stops inappropriate sales reminders (tested)
- Segment engine: all 15 segments, pure-function-of-events, recomputed on every scan
- 13 new tests (47 total passing)

## [0.3.0] — 2026-10-03 — Phase 2: Free Acquisition Engine

### Added
- Migration 0003: config_sources, raw_configs (unique uri_hash), published_posts (unique dedupe_key), exclusive_claims (unique claim_key)
- Public source registry + fetchers: GitHub raw files, Telegram public previews (t.me/s) — HTTP only, no crawling
- Config parsers vmess/vless/trojan/ss(SIP002) with credential-fingerprint dedup, validation (private/loopback hosts, port ranges, missing credentials), reliability-weighted scoring
- Limited health checking: capped TCP checks only (default max 5, concurrency 3, 4s timeout)
- Idempotent publishing per day+slot — double scheduler runs cannot double-post; unhealthy/invalid configs never publish; community-sourced labeling enforced
- PV exclusive pool: data-driven campaigns (traffic/validity/location/protocol/claims/per-user/date_overrides), idempotent claims, provisioning interface with Http/Fake clients, outage-safe queueing
- 15 new tests (34 total passing)

### Fixed
- In-transaction dedupe: staged pipeline inserts now use savepoints (autoflush-off safe)

## [0.2.0] — 2026-10-03 — Phase 1: Core Data / Events / Attribution / Mirza

### Added
- Migration 0002: users, campaigns, sources, events (unique idempotency_key), attribution_touch (unique user+kind)
- Event service: 23 normalized event types, concurrent-safe idempotent ingestion via savepoint + re-fetch
- Attribution engine: `start=` deep-link parser (freecfg/ref/partner/seo/channel/social/custom/direct);
  first-touch immutable, last-touch upserted; SOURCE_ATTRIBUTED deduped per user+source
- Read-only Mirza adapter (GET-only HTTP client, route table, FakeMirza for tests, NotConfigured degradation)
- Telegram Bot API client with injectable Http/Fake transport (messages, inline keyboards, channel posts, webhooks)
- POST/GET /api/events ingestion endpoint: admin-token auth, rate limiting, 201 + created=false on duplicates
- Tests: ingestion idempotency, unknown-type rejection, first-touch immutability, source persistence,
  API auth, Mirza read-only contract (13 new; 19 total passing)

## [0.1.0] — 2026-10-03 — Phase 0: Foundation & Safety

### Added
- FastAPI application skeleton with `/health` (liveness) and `/ready` (DB readiness, 503 on failure)
- `pv_growth.core` — env-driven settings (pydantic-settings), JSON structured logging, domain errors
- Feature-flag service: env defaults (all OFF) + DB overrides (`feature_flag` table) + TTL cache
- Database layer: SQLAlchemy 2 engine/session scopes, portable JSON (JSONB on PG) types
- Alembic environment + `0001_bootstrap` migration (feature_flag)
- Read-only production preflight CLI: protected paths/containers, docker networks,
  port & subnet collision, duplicate-instance, disk/memory minimums, PG reachability
- Protected-service smoke CLI: Mirza, PV reseller, AKH bot, Apache, X-UI, PostgreSQL, GrowthOS
- `scripts/sentinelx-cleanup.sh` — inspect-save then container-only removal (owner-authorized)
- Dockerfile (non-root, healthcheck) + docker-compose (384M/0.75CPU/200 pids, 127.0.0.1:8350 only)
- GitHub Actions CI: ruff, pytest matrix (sqlite/postgres), alembic up/down, pip-audit, docker build
- Tests: health/ready behavior, preflight port-parse and subnet-overlap logic
- Project documentation set (PLAN/STATUS/SECURITY/DEPLOYMENT/ROLLBACK/BLOCKERS/…)

### Safety
- No production resource touched; all server-side actions deferred as documented blockers
