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
Every migration ships a tested downgrade. Do not drop the GrowthOS database as
a routine rollback; restore the verified `/opt/pv-growth/backups` dump if data
recovery is needed. Mirza data is outside this database and must never be altered.

## Feature rollback (no redeploy)
Every module has a flag; admin API or DB row flips it OFF instantly:
```sql
UPDATE feature_flag SET enabled = false WHERE key = 'LIFECYCLE_AUTOMATION_ENABLED';
UPDATE feature_flag SET enabled = false WHERE key IN
  ('CONTENT_ENGINE_ENABLED', 'INSTAGRAM_AUTOMATION_ENABLED');
```
(flags cache TTL is 15 s)

For an Instagram incident, flip `INSTAGRAM_AUTOMATION_ENABLED` OFF first. This
stops new planning/publishing after the cache TTL without touching Telegram,
Mirza sync, provisioning, VPN tunnels, firewall or protected services.

## Instagram migration rollback (0008 → 0007)

Only after the Instagram/content flags are OFF and the previous application
image is selected:

```bash
docker run --rm --network host --env-file /opt/pv-growth/config/.env \
  <image-containing-0008> python -m alembic -c /app/alembic.ini downgrade 0007
```

This drops only GrowthOS `content_publications`/`content_insights` plus the new
`content_items.format`/`creative` fields. Take/verify the GrowthOS backup first.

## Phase-level rollback
Each phase's acceptance report lists the exact commit range; `git revert` the
phase commits, re-run migrations downgrade for that phase's revisions.

## SentinelX (Phase 0) rollback
Inspect snapshot stored by `scripts/sentinelx-cleanup.sh` under /opt/pv-growth/audit/.
Restore: `docker run -d --name sentinelx-worker --restart <policy> <image>`
(summary line captured in sentinelx-worker-summary-*.txt). Files at
/opt/sentinelx-worker were never deleted.
