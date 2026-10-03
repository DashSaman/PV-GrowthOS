# Changelog

All notable changes to PV GrowthOS. Format loosely follows Keep a Changelog.

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
