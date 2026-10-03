# SERVER_RESOURCE_MAP.md

From the completed production audit. Read-only reference — GrowthOS adds itself
in its own namespace and never takes ownership of anything below.

## Host
- Ubuntu 22.04 · 2 vCPU · ~4 GB RAM (no swap) · ~38 GB disk
- Docker, Apache, MySQL, PostgreSQL 14 installed; X-UI/Xray active;
  Hedioum + WireGuard/GRE/SIT tunnels active; real VPN customers onboard

## Docker networks (protected — never modify/attach)
| Network | Subnet |
|---|---|
| bridge | 172.17.0.0/16 |
| pv_reseller_net | 172.18.0.0/16 |
| akhbot_internal | 172.19.0.0/16 |

## Protected services
| Service | Path | Binding | Domain |
|---|---|---|---|
| Mirza (sales/provisioning) | /var/www/html/mirzaprobotconfig + /var/www/mirza_pro | — | robot.ahsg.top |
| PV Reseller Dashboard | /opt/pv-reseller | pv-reseller-dashboard 127.0.0.1:31080→3000 | npanel.softarg.ir |
| AKH Bot | /opt/akhbot/app | akhbot-app 127.0.0.1:8307→8000 | rasteh.softarg.ir |
| X-UI / Xray | — | — | — |
| Tunnels | WireGuard · GRE · SIT · Hedioum + watchdogs | — | — |

## Reserved TCP ports
22 53 80 443 465 993 1194 1195 1196 1197 2083 2087 2096 3000 3306 4020 5000
5432 8080 8307 8443 9090 11111 31080 33060 46573 62789
Reserved UDP: 22295 46414
Always re-verify with `ss -lntup` before allocating (preflight does this).

## Databases
| DB | Engine | Status |
|---|---|---|
| postgres, pvnaive | PostgreSQL 14 | protected; do not touch |
| pv_growth | PostgreSQL 14 | NEW — created by GrowthOS runbook, least-privilege user |
| hajsaman | MySQL | protected; never GrowthOS storage |

## GrowthOS namespace (new)
- Deploy root: /opt/pv-growth · Bind: 127.0.0.1:8350 · Network: pv_growth_net (172.23.77.0/24, preflight-verified) · Container: pv-growth-app (limits 384M/0.75CPU/200 pids)
