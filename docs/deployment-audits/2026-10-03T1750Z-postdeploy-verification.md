# Post-Deploy Verification — 2026-10-03T17:50Z

Deployed: `pv-growth-app` image `pv-growth-app:28e98a7` (commit 28e98a7) —
built OFF-server (workstation Docker), transferred via `docker save | load`.

## GrowthOS production state
| Check | Result |
|---|---|
| container | `Up (healthy)`, restart=unless-stopped |
| limits | mem 402653184 (384M) · cpus 0.75 · pids 200 |
| binding | 127.0.0.1:8350 only |
| /health | 200 `{"status":"ok","version":"1.0.0"}` |
| /ready | 200 `{"status":"ready","env":"production"}` |
| migrations | alembic head **0007** · 28 tables |
| feature flags | all 10 = false (passive stage) |
| admin auth | no token → 401 ✓ |
| scheduler | lifecycle.scan executing (done counter rising; handler-registry bug found & fixed, redeployed) |
| Mirza sync | LIVE: first cycle scanned 7,491 invoices, ingested 300 (286 PAYMENT_SUCCESS + SERVICE_CREATED pairs, 12 CHECKOUT_STARTED, 2 skipped); subsequent cycles continue at 300/cycle; failures isolated |
| analytics | funnel & revenue built from REAL Mirza payments |
| container resources | 64.4 MiB / 384 MiB · 0.32% CPU |

## Protected services — before → after (identical = PASS)
| Service | Before | After |
|---|---|---|
| Mirza (robot.ahsg.top) | 200 | **200** |
| PV Reseller (npanel + local) | 200 / 200 | **200 / 200** |
| AKH (rasteh + local) | 401 / 200 | **401 / 200** |
| Apache local | 200 · configtest OK | **200** |
| PostgreSQL / MySQL | active / active | **active / active** |
| X-UI / Xray / Hedioum | active / running / active | **unchanged** |
| WireGuard wgm3 / tunnels / watchdog | up | **up** |
| pv-gre.service | failed (pre-existing) | **unchanged (not touched)** |

Firewall: iptables/nft checksums changed ONLY by Docker's automatic rules for
the new 172.23.77.0/24 bridge (4 NAT rules) — expected platform behavior, no
manual firewall modification. Baseline checksums recorded pre-deploy.

## Host changes made (all GrowthOS-scoped, reversible)
1. `/swapfile` 2 GB + fstab + `vm.swappiness=10` (was: no swap) — rollback: swapoff + rm + remove fstab line
2. DB `pv_growth` + role `pv_growth` (least privilege) on existing PostgreSQL 14
3. MySQL role `pv_growth_ro` (SELECT on hajsaman.invoice/user only)
4. Docker network `pv_growth_net` (172.23.77.0/24 — verified collision-free)
5. systemd units `pv-growth-pgforward` / `pv-growth-mysqlforward` (socat, 172.23.77.1 → localhost)
6. `/opt/pv-growth/{config,logs,backups,deploy,audit}` (config/.env root:600)

## Backup & rollback (verified live)
- pg_dump pv_growth → gzip → `gzip -t` OK (42 KB first snapshot)
- Rehearsal: `docker stop pv-growth-app` → GrowthOS down, ALL protected services
  unchanged → `docker start` → healthy again.
- SentinelX: container absent on this host (previously removed) — nothing to do;
  recorded in SERVER_RESOURCE_MAP as REMOVED.

## Issues found & fixed during deploy (honest log)
1. `jobs/scheduler.py` missing (import crash when scheduler enabled) → module created + regression test; redeployed.
2. Handler registry empty at runtime (lifecycle.scan stuck pending) → `register_builtin_handlers()`; regression test; redeployed.
3. `docker restart` does not re-read `--env-file` → container recreated instead.
4. MySQL grant needed `localhost` form (socat makes clients appear local).
5. Mirza invoice PK is random hex (no numeric watermark) → sync redesigned to seen-set scan + event idempotency; forward-safe.
6. `sync` initially would have replayed all history → limit 300/cycle + seen-set.
