# Changelog

All notable changes to PV GrowthOS. Format loosely follows Keep a Changelog.

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
