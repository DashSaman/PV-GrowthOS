# PV GrowthOS production deployment

Production contract: **green main CI → immutable SHA artifact → checksum →
preflight → GrowthOS backup → dangerous flags OFF → migrate → replace only
`pv-growth-app` → health/ready → protected-service smoke → canaries or
GrowthOS-only rollback**.

The production host never builds the image. The canonical secrets file is
`/opt/pv-growth/config/.env` (`root:root`, mode `600`). Do not use `docker
compose down` on the server and do not modify Mirza, reseller/AKH containers,
Apache, Xray/X-UI configuration, tunnels, or firewall rules.

## Release artifact

The GitHub `main` workflow builds `pv-growth-app:<full-commit-sha>` only after
lint, SQLite/PostgreSQL tests, migration tests and `pip-audit` pass. It uploads
artifact `pv-growth-app-<sha>` containing:

```text
pv-growth-app-<sha>.tar
pv-growth-app-<sha>.tar.sha256
backup.sh
```

Download that exact artifact; never rebuild the release on production. Before
transfer/loading, verify the included checksum:

```bash
sha256sum -c pv-growth-app-<sha>.tar.sha256
```

Copy only those three release files to `/opt/pv-growth/releases/<sha>/` on the
server. Re-run `sha256sum -c` there before `docker load`.

## One-time host prerequisites

These are prerequisites, not routine rollout commands:

- existing PostgreSQL and GrowthOS-owned `pv_growth` DB/role;
- existing `pv_growth_net`, collision-checked by preflight;
- `/opt/pv-growth/config/.env` with GrowthOS-only credentials;
- direct-container smoke targets in that env point at the host gateway/public
  service rather than container-local loopback (`PVG_SMOKE_APACHE_URL=http://172.23.77.1:80`,
  `PVG_SMOKE_PG_HOST=172.23.77.1`, and `PVG_SMOKE_XUI_URL` is the live X-UI URL);
- Mirza MySQL credentials are the `pv_growth_ro` SELECT-only account;
- no production secret exists in Git or in the image.

If a prerequisite is missing, stop the rollout. Do not repair a protected
service as part of a GrowthOS deploy.

## Protected rollout

Set `SHA` to the exact green `main` commit used by the downloaded artifact.
Commands below intentionally address only the GrowthOS container/database.

```bash
cd /opt/pv-growth/releases/<sha>
sha256sum -c pv-growth-app-<sha>.tar.sha256
docker load --input pv-growth-app-<sha>.tar
docker image inspect pv-growth-app:<sha> --format '{{.Id}}'

# Host-only gate. These checks deliberately run on the host, not in the image.
for p in /var/www/html/mirzaprobotconfig /var/www/mirza_pro /opt/pv-reseller /opt/akhbot/app; do
  test -e "$p"
done
for c in pv-reseller-dashboard akhbot-app; do
  test "$(docker inspect -f '{{.State.Running}}' "$c")" = true
done
for n in bridge pv_reseller_net akhbot_internal pv_growth_net; do
  docker network inspect "$n" >/dev/null
done
test "$(docker network inspect pv_growth_net --format '{{(index .IPAM.Config 0).Subnet}}')" = "172.23.77.0/24"
test "$(docker inspect pv-growth-app --format '{{.State.Running}}')" = true
test "$(docker inspect pv-growth-app --format '{{(index (index .NetworkSettings.Ports "8350/tcp") 0).HostIp}}:{{(index (index .NetworkSettings.Ports "8350/tcp") 0).HostPort}}')" = "127.0.0.1:8350"

# Candidate-only gate: host Docker/path checks are reported SKIP because an
# image cannot see them. DB/resource checks must pass.
docker run --rm --network host \
  --env-file /opt/pv-growth/config/.env \
  pv-growth-app:<sha> python -m pv_growth preflight --container

# BEFORE evidence: current GrowthOS + protected neighbors.
docker inspect pv-growth-app --format '{{.Config.Image}}' > previous-image.txt
docker exec pv-growth-app python -m pv_growth smoke
```

Back up only the GrowthOS database using the existing live DB URL. `backup.sh`
verifies gzip before returning success:

```bash
export PVG_DATABASE_URL="$(docker exec pv-growth-app python -c \
  'from pv_growth.core.config import get_settings; print(get_settings().database_url)')"
bash /opt/pv-growth/releases/<sha>/backup.sh
unset PVG_DATABASE_URL
```

Capture current feature-flag state, then force the content/Instagram automation
kill switches off before any migration. A DB override wins over env defaults.

```bash
docker exec pv-growth-app python -c \
  "from pv_growth.core.config import get_settings; from sqlalchemy import create_engine,text; e=create_engine(get_settings().database_url); c=e.connect(); print(c.execute(text('select key,enabled from feature_flag order by key')).all()); c.close()"

docker exec pv-growth-app python -c \
  "from pv_growth.core.config import get_settings; from sqlalchemy import create_engine,text; e=create_engine(get_settings().database_url); c=e.begin(); x=c.__enter__(); [x.execute(text('insert into feature_flag(key,enabled) values (:k,false) on conflict(key) do update set enabled=false'), {'k': k}) for k in ('CONTENT_ENGINE_ENABLED','INSTAGRAM_AUTOMATION_ENABLED')]; c.__exit__(None,None,None)"
```

Run the candidate migrations to Alembic `0011`, then replace **only**
`pv-growth-app`:

```bash
docker run --rm --network host \
  --env-file /opt/pv-growth/config/.env \
  pv-growth-app:<sha> python -m pv_growth migrate

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
  pv-growth-app:<sha>

curl -fsS http://127.0.0.1:8350/health
curl -fsS http://127.0.0.1:8350/ready
docker exec pv-growth-app python -m pv_growth smoke
```

If `/health`, `/ready`, or any protected-service smoke changes from the
pre-deploy baseline, stop and follow `ROLLBACK.md`. Never "fix" the protected
neighbor.

Re-enable only core flags that were already live before this deploy. Do not
enable a newly introduced sales/partner/content surface merely because the
application is healthy.

## Sales-loop validation

The release is meant to improve conversion, not merely keep a container up.
After the technical smoke, verify the existing live paths remain observable:

1. Mirza import remains SELECT-only and new settled payments become normalized
   payment/conversion evidence once.
2. Lifecycle queue remains bounded; stop conditions suppress sales reminders
   after purchase and ambiguous Telegram delivery is never blindly resent.
3. Referral/partner credit is created only from proven settled conversion
   data; unknown commercial values stay unresolved.
4. Free-config/exclusive claims respect quota and only confirmed provisioning
   produces `TRIAL_CREATED`.
5. Admin funnel/source/revenue views are used to compare bot-start → trial →
   payment conversion rather than optimizing vanity metrics.

These checks preserve the revenue feedback loop while avoiding fabricated
sales attribution.

## Instagram one-time configuration and canary

`CONTENT_ENGINE_ENABLED` and `INSTAGRAM_AUTOMATION_ENABLED` stay OFF until the
account is a Meta-supported Professional account and all readiness inputs are
real. Secrets belong only in `/opt/pv-growth/config/.env`:

```text
PVG_INSTAGRAM_ACCOUNT_ID=...
PVG_INSTAGRAM_ACCESS_TOKEN=...
PVG_INSTAGRAM_API_VERSION=...      # explicit supported version; never guessed
PVG_MEDIA_PUBLIC_BASE_URL=https://...
PVG_MEDIA_STORE_ROOT=/...          # actually served by the public media origin
PVG_MEDIA_RETENTION_HOURS=48
```

`GET /admin/api/instagram/readiness` returns only booleans/missing key names;
it never exposes tokens. The public media origin must already exist; this
rollout does not edit protected Apache/Mirza routes.

Canary order:

1. `POST /admin/api/instagram/plan/preview`; inspect facts and CTA. Preview is
   rolled back and creates no content/publication row.
2. Enable `CONTENT_ENGINE_ENABLED`, then `INSTAGRAM_AUTOMATION_ENABLED` for one
   canary window only after readiness is green.
3. Verify exactly one post/reel reaches `published` with its real remote media
   id. `publish_unknown` is reconciled only by a unique owned-media marker
   `socialc_<content_id>` within the bounded time window; it is never blindly
   republished. Stories remain fail-closed when publication is ambiguous.
4. Open the CTA and verify `socialc_<content_id>` is recorded as exact source
   `socialc:<content_id>` with the owning campaign retained.
5. Verify an insight snapshot for the exact content item. Optimize on
   purchase/trial evidence, not reach alone.
6. Verify post, reel and story independently before expanding the mix.

Meta controls Explore/feed distribution. No implementation can guarantee that
every post enters Explore; this system automates supported publishing,
attribution, measurement and iteration toward sales.

## Resource policy

- 384 MB memory, 0.75 CPU, 200 pids; localhost port `127.0.0.1:8350` only.
- JSON logs rotate at 10 MB × 3.
- No image build on the production host.
- `docker-compose.yml` is for local/rehearsal parity only, not the production
  replacement mechanism.
- Keep the previous GrowthOS image until the post-deploy audit is complete.

Rollback: see `ROLLBACK.md`.
