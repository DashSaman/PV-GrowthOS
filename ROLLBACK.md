# PV GrowthOS rollback

Golden rule: if a protected-service smoke check regresses after a GrowthOS
action, roll back **GrowthOS only**. Never restart, reconfigure or repair Mirza,
reseller/AKH, Apache, Xray/X-UI, tunnels, firewall or unrelated containers as
part of this rollback.

## Immediate feature kill switch

For an automation incident, disable the smallest affected GrowthOS surface
first. DB overrides win over environment defaults; flag cache TTL is 15 seconds.

```sql
UPDATE feature_flag SET enabled = false
WHERE key = 'LIFECYCLE_AUTOMATION_ENABLED';

UPDATE feature_flag SET enabled = false
WHERE key IN ('CONTENT_ENGINE_ENABLED', 'INSTAGRAM_AUTOMATION_ENABLED');
```

Instagram ambiguity is fail-closed: disabling its flag stops new work; do not
retry a `publish_unknown` item manually unless reconciliation proves no remote
publication exists.

## Application rollback

`DEPLOYMENT.md` records the previous image in `previous-image.txt` before
replacement. Use that exact GrowthOS image; do not use `docker compose down`.

```bash
docker stop pv-growth-app
docker rm pv-growth-app
docker run -d --name pv-growth-app --restart unless-stopped \
  --network pv_growth_net \
  --env-file /opt/pv-growth/config/.env \
  --memory 384m --cpus 0.75 --pids-limit 200 \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  --health-cmd="python -c \"import httpx;httpx.get('http://127.0.0.1:8350/health',timeout=4).raise_for_status()\"" \
  --health-interval=30s --health-timeout=5s --health-retries=3 --health-start-period=15s \
  -p 127.0.0.1:8350:8350 \
  <previous-growthos-image>

curl -fsS http://127.0.0.1:8350/health
curl -fsS http://127.0.0.1:8350/ready
docker exec pv-growth-app python -m pv_growth smoke
```

If the previous application cannot run against the migrated schema, stop it
and perform the database rollback below before restarting it. For the current
`0007 → 0011` production rollout, the rollback target is `0007`.

## Database rollback

Database downgrade is not the first response to an app-only failure. First
verify the GrowthOS backup created by `DEPLOYMENT.md`. With the application
stopped and content/Instagram flags off:

```bash
docker run --rm --network host \
  --env-file /opt/pv-growth/config/.env \
  <candidate-image-containing-0011> \
  python -m alembic -c /app/alembic.ini downgrade 0007
```

Revisions `0011`, `0010`, `0009`, and `0008` contain tested downgrades. The
downgrade intentionally removes their GrowthOS-only schema/data, so keep the
pre-deploy backup. Never drop the GrowthOS database as a routine rollback and
never restore/write Mirza data.

If data recovery (not schema compatibility) is required, restore only the
verified GrowthOS dump under `/opt/pv-growth/backups` using the project backup
procedure.

## Rollback completion criteria

Rollback is complete only when:

- `/health` and `/ready` are healthy on `127.0.0.1:8350`;
- the protected-service smoke matches the pre-deploy baseline;
- the active GrowthOS image and Alembic revision are recorded;
- the dangerous feature flags remain off until the incident is understood.

Do not delete the failed candidate image or audit evidence until the cause is
recorded.
