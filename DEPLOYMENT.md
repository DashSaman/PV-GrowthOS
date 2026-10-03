# DEPLOYMENT.md

Deployment contract: preflight → backup state → pull tested image → migration
→ start → health check → protected-service smoke → success or automatic rollback.

## One-time server preparation (owner, ~10 min)

```bash
# 1. database (existing PostgreSQL 14 — no new install)
sudo -u postgres psql <<'SQL'
CREATE USER pv_growth WITH PASSWORD '<strong-password>';
CREATE DATABASE pv_growth OWNER pv_growth;
SQL

# 2. namespace
sudo mkdir -p /opt/pv-growth && cd /opt/pv-growth
sudo git clone https://github.com/DashSaman/PV-GrowthOS.git repo

# 3. environment (secrets — root-owned)
sudo tee /opt/pv-growth/.env >/dev/null <<'ENV'
PVG_ENV=production
PVG_DATABASE_URL=postgresql+psycopg://pv_growth:<strong-password>@127.0.0.1:5432/pv_growth
PVG_ADMIN_TOKEN=<admin-token>
PVG_TELEGRAM_BOT_TOKEN=<token>
PVG_FREE_CHANNEL_ID=<id>
PVG_OFFICIAL_CHANNEL_ID=<id>
PVG_SCHEDULER_ENABLED=1
ENV
sudo chmod 600 /opt/pv-growth/.env

# 4. dedicated network (subnet verified free by preflight in step 5)
docker network create pv_growth_net --subnet 172.23.77.0/24

# 5. READ-ONLY preflight (aborts on any FAIL)
docker run --rm --network host -v /opt/pv-growth/.env:/opt/pv-growth/.env:ro \
  --env-file /opt/pv-growth/.env pv-growth-app:<tag> python -m pv_growth preflight

# 6. sentinelx cleanup (owner-authorized; dry-run first)
sudo bash /opt/pv-growth/repo/scripts/sentinelx-cleanup.sh /opt/pv-growth/audit
sudo CONFIRM=yes bash /opt/pv-growth/repo/scripts/sentinelx-cleanup.sh /opt/pv-growth/audit
```

## Routine deploy (after CI builds the image)

```bash
cd /opt/pv-growth
python -m pv_growth smoke                    # BEFORE: all protected services healthy?
docker pull <registry>/pv-growth-app:<new-sha>
docker stop pv-growth-app && docker rm pv-growth-app
docker run -d --name pv-growth-app --restart unless-stopped \
  --network pv_growth_net --env-file /opt/pv-growth/.env \
  --memory 384m --cpus 0.75 --pids-limit 200 \
  -p 127.0.0.1:8350:8350 <registry>/pv-growth-app:<new-sha>
docker exec pv-growth-app python -m pv_growth migrate
curl -fsS http://127.0.0.1:8350/ready
python -m pv_growth smoke                    # AFTER: rollback if anything fails
```

## Rollback
See ROLLBACK.md. Short version: `docker compose down`, restore previous tag, re-smoke.

## Resource policy
- Container limits are mandatory: 384 MB memory, 0.75 CPU, 200 pids.
- No image builds on this host. CI builds; the server only pulls.
- Logs: json-file driver, 10 MB × 3 rotations.
