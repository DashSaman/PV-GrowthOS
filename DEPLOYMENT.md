# DEPLOYMENT.md

Deployment contract: preflight → backup state → pull tested immutable image →
migration → start → health check → protected-service smoke → success or rollback.

## One-time server preparation (owner, ~10 min)

```bash
# 1. database (existing PostgreSQL 14 — no new install)
sudo -u postgres psql <<'SQL'
CREATE USER pv_growth WITH PASSWORD '<strong-password>';
CREATE DATABASE pv_growth OWNER pv_growth;
SQL

# 2. namespace
sudo mkdir -p /opt/pv-growth/config && cd /opt/pv-growth
sudo git clone https://github.com/DashSaman/PV-GrowthOS.git repo

# 3. environment (secrets — root-owned)
sudo tee /opt/pv-growth/config/.env >/dev/null <<'ENV'
PVG_ENV=production
PVG_DATABASE_URL=postgresql+psycopg://pv_growth:<strong-password>@127.0.0.1:5432/pv_growth
PVG_ADMIN_TOKEN=<admin-token>
PVG_TELEGRAM_BOT_TOKEN=<token>
PVG_FREE_CHANNEL_ID=<id>
PVG_OFFICIAL_CHANNEL_ID=<id>
PVG_SCHEDULER_ENABLED=1
ENV
sudo chmod 600 /opt/pv-growth/config/.env

# 4. dedicated network (subnet verified free by preflight in step 5)
docker network create pv_growth_net --subnet 172.23.77.0/24

# 5. READ-ONLY preflight (aborts on any FAIL)
docker run --rm --network host \
  --env-file /opt/pv-growth/config/.env pv-growth-app:<tag> python -m pv_growth preflight

# 6. sentinelx cleanup (owner-authorized; dry-run first)
sudo bash /opt/pv-growth/repo/scripts/sentinelx-cleanup.sh /opt/pv-growth/audit
sudo CONFIRM=yes bash /opt/pv-growth/repo/scripts/sentinelx-cleanup.sh /opt/pv-growth/audit
```

## Routine deploy (after CI/off-server build produces the image)

```bash
cd /opt/pv-growth
export PVG_DATABASE_URL="$(docker exec pv-growth-app python -c \
  'from pv_growth.core.config import get_settings; print(get_settings().database_url)')"
bash /opt/pv-growth/repo/scripts/backup.sh   # pv_growth only; gzip verified
unset PVG_DATABASE_URL
docker exec pv-growth-app python -m pv_growth smoke # BEFORE: protected services healthy?
docker pull <registry>/pv-growth-app:<new-sha>

# Instagram rollout invariant: dangerous automation is OFF before migration.
docker exec pv-growth-app python - <<'PY'
from pv_growth.core.config import get_settings
from sqlalchemy import create_engine, text
e = create_engine(get_settings().database_url)
with e.begin() as c:
    for key in ("CONTENT_ENGINE_ENABLED", "INSTAGRAM_AUTOMATION_ENABLED"):
        c.execute(text("INSERT INTO feature_flag(key,enabled) VALUES (:k,false) "
                       "ON CONFLICT(key) DO UPDATE SET enabled=false"), {"k": key})
PY

# 0008 is backward-compatible with the previous app. Migrate before replacement.
docker run --rm --network host --env-file /opt/pv-growth/config/.env \
  <registry>/pv-growth-app:<new-sha> python -m pv_growth migrate
docker stop pv-growth-app && docker rm pv-growth-app
docker run -d --name pv-growth-app --restart unless-stopped \
  --network pv_growth_net --env-file /opt/pv-growth/config/.env \
  --memory 384m --cpus 0.75 --pids-limit 200 \
  -p 127.0.0.1:8350:8350 <registry>/pv-growth-app:<new-sha>
curl -fsS http://127.0.0.1:8350/ready
docker exec pv-growth-app python -m pv_growth smoke # AFTER: rollback if anything fails
```

Never build the production candidate on the 4 GB VPN host. If no off-server
image can be transferred/pulled, deployment is **BLOCKED** rather than replaced
with an ad-hoc mutable-container update.

## Instagram one-time configuration and canary

Keep both flags OFF until the Instagram account is a Meta-supported Professional
account and the following values exist only in `/opt/pv-growth/config/.env`:

```text
PVG_INSTAGRAM_ACCOUNT_ID=...
PVG_INSTAGRAM_ACCESS_TOKEN=...
PVG_INSTAGRAM_API_VERSION=...      # explicit supported version; never guessed
PVG_MEDIA_PUBLIC_BASE_URL=https://...
PVG_MEDIA_STORE_ROOT=/...          # directory actually served by that media origin
PVG_MEDIA_RETENTION_HOURS=48
```

`GET /admin/api/instagram/readiness` returns only booleans/missing key names;
it never returns tokens. The public media origin must already exist — this
rollout does not modify Apache/Mirza routes.

Canary order:

1. `POST /admin/api/instagram/plan/preview`; inspect facts/CTA. This preview is
   rolled back and creates no content/publication rows.
2. Enable `CONTENT_ENGINE_ENABLED`, then `INSTAGRAM_AUTOMATION_ENABLED` for a
   single canary window only after readiness is green.
3. Verify exactly one `content_publications` row reaches `published` and has a
   remote `container_id` + `media_id`. Ambiguous publish results stay
   `publish_unknown` and are never blindly repeated.
4. Open the CTA and verify `social_<campaign>` becomes source
   `social:<campaign>` in GrowthOS attribution.
5. Verify a `content_insights` snapshot. Only then leave that format enabled.
6. Verify post, reel and story separately before expanding the automatic mix.

Meta controls Explore/feed distribution. A successful canary proves supported
publication/measurement, not guaranteed Explore placement.

## Rollback
See ROLLBACK.md. Short version: `docker compose down`, restore previous tag, re-smoke.

## Resource policy
- Container limits are mandatory: 384 MB memory, 0.75 CPU, 200 pids.
- No image builds on this host. CI/off-server builds; the server only pulls/loads.
- Logs: json-file driver, 10 MB × 3 rotations.
