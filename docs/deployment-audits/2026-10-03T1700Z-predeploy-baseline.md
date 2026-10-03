# Production Baseline — READ-ONLY — 2026-10-03T17:00Z (pre-deploy)

Collected over SSH (authorized key) immediately before any GrowthOS change.
No secrets recorded. Purpose: before/after comparison for the deploy gate.

## Host
- hostname: `RoboT` · Ubuntu 22.04 · 2 vCPU · 3815 MB RAM · disk 38G (20G used, 17G free, 55%)
- uptime: 2 days · load: 0.34 0.29 0.37
- swap: **none** (swapon --show empty)

## Protected services — BEFORE status
| Service | State | Evidence |
|---|---|---|
| Apache | active, `apache2ctl configtest`: Syntax OK | local :80 → 200 |
| PostgreSQL 14.24 | active | databases: postgres, pvnaive, template0/1 (no pv_growth yet) |
| MySQL | active | Uptime 186633s, 21.5 qps |
| X-UI | active (`x-ui.service`) | — |
| Xray | process running (xray-linux-amd64, pid 1144036, under x-ui) | — |
| Hedioum | active (`hedioum.service`) | — |
| WireGuard | interface wgm3 up, listen 46414/udp | — |
| pv-gre-watchdog | active | — |
| pv-gre.service | **failed** (pre-existing, documented in EXISTING_ISSUES.md) | — |
| Mirza | https://robot.ahsg.top → **200** | — |
| PV Reseller | https://npanel.softarg.ir → **200** · local 127.0.0.1:31080 → **200** | container Up 2 days |
| AKH Bot | https://rasteh.softarg.ir → **401** (auth-protected, alive) · local 127.0.0.1:8307 → **200** | container Up 38m (healthy) |

## Docker
- containers: akhbot-app, pv-reseller-dashboard (both Up)
- networks: akhbot_internal, bridge, host, none, pv_reseller_net
- **sentinelx-worker: container ABSENT (previously removed — no action needed)**
- no /opt/sentinelx-worker on this host

## Network snapshot (for regression comparison)
- iptables-save md5: `889f677f63f077681f9c4f127acf709a`
- ip6tables-save md5: `d6fccc99cf05ade80a9e2dc2bb299f18`
- nft ruleset md5: `11a42a774085a68f73e90067a96235b2`
- ip rule: default 3 rules (0/32766/32767)
- routes include tunnel nets 10.15.16.0/30 (grepv), 10.66.66.0/30 (wgm3), 10.243.249.204/30 (hw771), docker 172.17/18/19
- **172.23.0.0/16 not present in any route → 172.23.77.0/24 is collision-free**
- **port 8350 free** (ss shows no listener)

## GrowthOS proposed deployment (verified against this baseline)
- host bind 127.0.0.1:8350 ✓ free · network pv_growth_net 172.23.77.0/24 ✓ free
- disk headroom 17G ✓ · available RAM ~2100MB ✓ (container capped at 384M)
