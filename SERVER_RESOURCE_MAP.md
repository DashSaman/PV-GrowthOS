# SERVER_RESOURCE_MAP.md — FINAL STATE (2026-10-03, post-deploy)

Host: `RoboT` · Ubuntu 22.04 · 2 vCPU · ~4 GB RAM (+ **2 GB swap — NEW, GrowthOS-initiated, persistent**) · 38 GB disk (17 GB free)

## Resource ownership

| Resource | Path / Binding | Owner | Status |
|---|---|---|---|
| Mirza (sales bot) | /var/www/html/mirzaprobotconfig + /var/www/mirza_pro · robot.ahsg.top | Mirza | **PROTECTED** — untouched; GrowthOS reads hajsaman.invoice/user SELECT-only |
| PV Reseller Dashboard | /opt/pv-reseller · pv-reseller-dashboard · 127.0.0.1:31080→3000 · pv_reseller_net · npanel.softarg.ir | PV Reseller | **PROTECTED** — 200→200 across deploy |
| AKH Bot | /opt/akhbot/app · akhbot-app · 127.0.0.1:8307→8000 · akhbot_internal · rasteh.softarg.ir | AKH | **PROTECTED** — 401→401 across deploy |
| X-UI / Xray | x-ui.service active · xray process | VPN infra | **PROTECTED** |
| Hedioum / WG / GRE / SIT / watchdogs | hedioum.service · wgm3 (:46414/udp) · grepv · pv-gre-watchdog | VPN infra | **PROTECTED** (pv-gre.service pre-existing failed — EXISTING_ISSUES.md) |
| Apache | vhosts unchanged; GrowthOS stays localhost-only | shared | **PROTECTED** |
| PostgreSQL 14 | 127.0.0.1:5432 · DBs postgres/pvnaive | shared | **PROTECTED**; new DB `pv_growth` (GROWTHOS OWNED) + least-privilege role `pv_growth` |
| MySQL | 127.0.0.1:3306 · hajsaman (Mirza) | Mirza | **PROTECTED**; read-only role `pv_growth_ro` (SELECT on invoice/user only) |
| Docker networks | bridge/pv_reseller_net/akhbot_internal | shared | **PROTECTED**; new `pv_growth_net` 172.23.77.0/24 (GROWTHOS OWNED) |
| sentinelx-worker | — | — | **REMOVED** (container absent before this deploy; no /opt/sentinelx-worker on this host) |
| GrowthOS app | container `pv-growth-app` · 127.0.0.1:8350 · image pv-growth-app:<sha> · limits 384M/0.75CPU/200pids | GrowthOS | **GROWTHOS OWNED — DEPLOYED, HEALTHY** |
| X-UI panel API (provisioning reuse) | v1.pvnetwork.ir:2087/Pikniki + :4020 sub · Bearer token in /opt/pv-growth/config/.env · growth-* client namespace only | shared (Mirza's interface) | **REUSED read+scoped-write** (temp free clients only; paid users untouched) |
| GrowthOS config/audit/backups | /opt/pv-growth/{config,logs,backups,deploy,audit} | GrowthOS | **GROWTHOS OWNED** (.env root-owned 600) |
| socat forwarders | pv-growth-pgforward (172.23.77.1:5432→127.0.0.1:5432) · pv-growth-mysqlforward (…:3306→…:3306) | GrowthOS | **GROWTHOS OWNED — NEW** (enabled, Restart=always) |
| swap | /swapfile 2 GB · fstab + sysctl swappiness=10 | host | **NEW (GrowthOS-initiated)** — rollback: `swapoff /swapfile && rm /swapfile`, remove fstab line |

## Reserved ports (verified live 2026-10-03)
TCP: 22 53 80 443 465 993 1194-1197 2083 2087 2096 3000 3306 4020 5000 5432 8080 8307 8443 9090 11111 31080 33060 46573 62789 **8350 (GrowthOS, localhost-only)**
UDP: 22295* 46414 (WireGuard wgm3) 1194-1197
