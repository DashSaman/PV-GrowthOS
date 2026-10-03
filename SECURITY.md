# SECURITY.md

## Secrets
- Never in Git: tokens, passwords, private keys, DB credentials, Telegram bot
  tokens, payment credentials. Everything arrives via environment (`PVG_*`) or
  the protected `.env` on the server at `/opt/pv-growth/.env` (chmod 600, root-owned).
- `.gitignore` blocks `.env*`; CI injects only throwaway test values.

## Network
- GrowthOS binds `127.0.0.1:8350` only — never `0.0.0.0`.
- Owns only `pv_growth_net`; never attached to `bridge`, `pv_reseller_net`, `akhbot_internal`.
- Admin API requires `X-Admin-Token` (constant-time compare); disabled entirely
  (403 on every route) when `PVG_ADMIN_TOKEN` is unset.

## Database
- Dedicated least-privilege role on the existing PostgreSQL 14 (see DEPLOYMENT.md):
  only database `pv_growth`, no superuser, no Mirza databases.
- All SQL via SQLAlchemy parameters — no string interpolation.
- Alembic migrations are additive and backward compatible where possible.

## Application
- Input validation at every API boundary (pydantic models).
- Rate limiting on ingestion/webhook endpoints (per-source token bucket, in-process).
- No customer VPN credentials in logs or event metadata (spec §31).
- Structured security events (auth failures, rate-limit hits) via JSON logs.
- CSRF: admin API is token-header based (not cookie based), which immune-by-design
  the browser-CSRF class for those routes; webhook endpoints are secret-verified.

## Forbidden destructive commands
docker system/network/volume prune · `docker rm $(docker ps ...)` · `iptables -F` ·
`nft flush ruleset` · `git reset --hard` on unrelated repos · broad `rm -rf`.
Only GrowthOS-owned resources are ever targeted.

## Reporting
Security review checklist runs in Phase 7 (dependency scan via pip-audit in CI
on every push; manual review of auth surface before rollout).
