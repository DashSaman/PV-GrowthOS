# ROLLBACK.md

Golden rule: if any protected-service smoke check fails AFTER a GrowthOS
action → roll back GrowthOS immediately. Never "fix" the protected service.

## Application rollback (deploy-level)
```bash
cd /opt/pv-growth
docker compose down              # stops pv-growth-app only; protected services untouched
docker run --rm <previous-image> # or: deploy the previously tagged image
docker compose up -d
python -m pv_growth smoke        # verify neighbors + GrowthOS
```
Keep the last 3 image tags on the host (`pv-growth-app:<sha>`); DEPLOYMENT.md tags each deploy.

## Database rollback (migration-level)
```bash
PVG_DATABASE_URL=... python -m alembic -c alembic.ini downgrade -1
```
Every migration ships a tested downgrade. GrowthOS data is non-critical
regeneration-able analytics state: worst case, `drop database pv_growth` +
re-migrate loses only GrowthOS events, never Mirza data.

## Feature rollback (no redeploy)
Every module has a flag; admin API or DB row flips it OFF instantly:
```sql
UPDATE feature_flag SET enabled = false WHERE key = 'LIFECYCLE_AUTOMATION_ENABLED';
```
(flags cache TTL is 15 s)

## Phase-level rollback
Each phase's acceptance report lists the exact commit range; `git revert` the
phase commits, re-run migrations downgrade for that phase's revisions.

## SentinelX (Phase 0) rollback
Inspect snapshot stored by `scripts/sentinelx-cleanup.sh` under /opt/pv-growth/audit/.
Restore: `docker run -d --name sentinelx-worker --restart <policy> <image>`
(summary line captured in sentinelx-worker-summary-*.txt). Files at
/opt/sentinelx-worker were never deleted.
