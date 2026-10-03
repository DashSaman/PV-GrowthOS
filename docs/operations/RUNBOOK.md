# Operations Runbook — PV GrowthOS

Production host: `RoboT` (91.107.240.235). GrowthOS container: `pv-growth-app`,
localhost `127.0.0.1:8350`, DB `pv_growth` on the shared PostgreSQL 14.

## Daily health
```bash
curl -s http://127.0.0.1:8350/health   # {"status":"ok"}
curl -s http://127.0.0.1:8350/ready    # {"status":"ready","env":"production"}
docker ps --filter name=pv-growth-app           # Up (healthy)
docker stats --no-stream pv-growth-app          # expect ~64M/384M, <1% CPU
TOKEN=$(grep PVG_ADMIN_TOKEN /opt/pv-growth/config/.env | cut -d= -f2)
curl -s -H "X-Admin-Token: $TOKEN" http://127.0.0.1:8350/admin/api/dashboard
```

## Restart GrowthOS ONLY (never docker restart policy of others)
```bash
docker restart pv-growth-app          # safe: jobs are durable, restart-safe
```

## Logs
```bash
docker logs --tail 100 pv-growth-app             # JSON structured
docker logs pv-growth-app 2>&1 | grep -i error
```

## Failed jobs
Dashboard → `system.failed_jobs`. Jobs retry with exponential backoff
(60s→1h, max 5 attempts) then dead-end as `failed`. Inspect:
```bash
docker exec pv-growth-app python - <<'PY'
from pv_growth.core.config import get_settings
from pv_growth.database.base import session_scope
from pv_growth.database.models import Job
from sqlalchemy import select
with session_scope(get_settings()) as s:
    for j in s.execute(select(Job).where(Job.status=="failed")).scalars():
        print(j.id, j.job_type, j.last_error)
PY
```
Re-run a failed job: set `status='pending', retry_after=NULL, attempt=0`.

## Pause ALL automation (emergency)
```sql
UPDATE feature_flag SET enabled=false;  -- effects stop within 15s (flag cache)
```
Or per-module, e.g. `... WHERE key='LIFECYCLE_AUTOMATION_ENABLED';`

## Pause free-config posting only
Flag `FREE_CONFIG_ENABLED=false` (and/or `PUBLIC_CONFIG_ENABLED`,
`PV_EXCLUSIVE_CONFIG_ENABLED`).

## Telegram failure handling
Webhook returns 503 while `PVG_TELEGRAM_BOT_TOKEN` is unset — passive mode is
intentional. Message-send failures are recorded on `message_log.status='failed'`
and retried by job backoff; they never crash the scheduler.

## Database health / disk
```bash
df -h / && free -m && docker exec pv-growth-app python -m pv_growth preflight
```

## Disk full
1. `docker logs` capped at 3×10MB by config.
2. Rotate backups: `ls -lat /opt/pv-growth/backups | head` — keep newest 14.
3. Never run `docker system prune`.

## High CPU/RAM
Container is hard-capped (384M/0.75 CPU/200 pids) — host services keep
headroom. If the cap is hit, GrowthOS throttles, it cannot starve VPN/Mirza.

## PV Exclusive provisioning (B5)
- Adapter: `XUIProvisioningAdapter` → Mirza's own X-UI panel API
  (`PVG_PROVISIONING_BASE_URL`/`TOKEN`/`INBOUND_IDS`/`SUBLINK` in .env)
- Free clients use the `growth-*` email namespace only — paid users can never collide
- Daily free budget: `PVG_FREE_DAILY_BUDGET` (default 25); guard also requires
  DB + panel health before every provision (fail closed)
- Expiry sweep (`provisioning.sweep`, every 5 min): marks expired claims and emits
  SERVICE_EXPIRED only when the panel confirms the service is gone/expired
- Manual check:
```bash
docker exec pv-growth-app python -c "
from pv_growth.core.config import get_settings
from pv_growth.provisioning.xui import get_provisioning
a=get_provisioning(get_settings())
print('health:', a.health())
print(a.service_state('growth-XXXX'))"
```

## Rollback
```bash
docker stop pv-growth-app && docker rm pv-growth-app
docker run -d --name pv-growth-app ... pv-growth-app:<previous-sha>   # see /opt/pv-growth/deploy notes
# DB: PVG_DATABASE_URL=... python -m alembic -c alembic.ini downgrade -1
```
Protected services are NEVER touched during GrowthOS rollback.

## Reboot safety (verified without rebooting)
- pv-growth-app: `restart=unless-stopped`
- forwarders pv-growth-pgforward/mysqlforward: `enabled`, `Restart=always`,
  `PartOf=docker.service`
- PostgreSQL/MySQL/Apache/X-UI/Hedioum/watchdogs: pre-existing enabled units
- swap: /etc/fstab + sysctl.d — persistent
