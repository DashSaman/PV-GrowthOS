<div align="center">

# PV GrowthOS

**Autonomous, production-safe growth system for PV Network — designed, implemented, and running in production**

سیستم رشد خودکار و ایمن PV Network — طراحی‌شده، پیاده‌سازی‌شده و **در حال اجرا روی سرور اصلی**

[![CI](https://github.com/DashSaman/PV-GrowthOS/actions/workflows/ci.yml/badge.svg)](https://github.com/DashSaman/PV-GrowthOS/actions)
![Production](https://img.shields.io/badge/production-live%20%40%20RoboT-10b981)
![Tests](https://img.shields.io/badge/tests-82%20passing-3b82f6)
![Version](https://img.shields.io/badge/release-v1.0.0-8b5cf6)

**v1.0.0** · Modular Monolith · FastAPI · SQLAlchemy 2 · PostgreSQL 14 · Telegram-native

[English](#-what-is-pv-growthos) | [فارسی](README.fa.md) | [Runbook](docs/operations/RUNBOOK.md) | [Persian PDF Report](docs/report/PV-GrowthOS-Report-FA.pdf)

</div>

---

## 🇬🇧 What is PV GrowthOS?

PV GrowthOS grows PV Network sales and long-term customer value with minimal
manual operation — while running **beside** the live Mirza sales bot, PV
Reseller Dashboard, AKH Bot and X-UI/Xray VPN infrastructure **without
touching them**. Mirza stays the source of truth; GrowthOS observes, attributes,
automates follow-ups and measures.

**One business objective** — every feature strengthens the funnel:

```
Acquisition → Bot Start → Free Config/Trial → Connection → Pricing
→ Checkout → Payment → Active Customer → Renewal → Referral
→ Affiliate/Reseller → Win-back
```

## 🟢 Current Production Status (2026-10-03)

| Aspect | State |
|---|---|
| Deployed | **YES** — container `pv-growth-app` on host `RoboT` (91.107.240.235), image built off-server, never on the server |
| Binding | `127.0.0.1:8350` only (never 0.0.0.0) |
| Network | dedicated `pv_growth_net` (172.23.77.0/24 — collision-checked live) |
| Limits | 384 MB mem / 0.75 CPU / 200 pids — actual usage ≈ 64 MB, <1% CPU |
| Database | `pv_growth` on the existing PostgreSQL 14 · Alembic head **0007** (28 tables) · least-privilege role |
| Mirza integration | **LIVE read-only, 100% reconciled**: 7,272 payments + 213 checkouts + 7,032 renewals ingested from 7,496 invoices via role `pv_growth_ro` (SELECT on `hajsaman.invoice/user` only) · `SERVICE_EXPIRED` change-detection on observed `active→disabledn` transitions |
| Telegram | **LIVE**: dedicated bot [@pvgrowthos_bot](https://t.me/pvgrowthos_bot) + channel [PV Network | Free Config](https://t.me/pvnetwork_freeconfig) (bot = admin) · long-polling (localhost-only topology, one mechanism) · deep-link attribution `freecfg_*` verified end-to-end |
| Free Config | **LIVE pipeline**: 1,846 real configs staged from an approved public aggregator; one health-checked config published to the channel; exclusive campaign `exc_e2e` claimed idempotently (1 GB/24h quota) |
| Referral | **LIVE E2E**: A→link→B→settled payment→exactly 1 reward; self-referral + duplicate reward + duplicate payment all rejected |
| Lifecycle | **canary verified**: real onboarding message delivered to a paying user; strict fact-validation refused 240 sends lacking real numbers (safety worked) |
| Feature flags | 6/10 ON: attribution, free-config, public, exclusive, lifecycle, referral · partner/content/competitor/experiments remain OFF (per rollout plan) |
| Protected services | Mirza 200→200 · Reseller 200→200 · AKH 401→401 · Apache/X-UI/Xray/Hedioum/tunnels: identical before/after (audited) |
| Tests / CI | 79 tests green · GitHub Actions green (lint, sqlite+postgres matrix, migrations up/down, pip-audit, docker build) |
| Swap | 2 GB swapfile added (was none) — persistent, conservative |
| Backup/rollback | pg_dump backup verified · rollback rehearsed live (stop → protected services unaffected → start) |
| Blocked (external) | dedicated Telegram bot token (webhook/publishing/lifecycle messaging inactive until provided) — see [BLOCKERS.md](BLOCKERS.md) |

## Growth Funnel

![Growth Funnel](docs/images/funnel.png)

## System Architecture

![Architecture](docs/images/architecture.png)

**Modular monolith** `pv-growth-app` — 23 modules behind clean seams: `core`,
`database`, `events`, `attribution`, `mirza_adapter`, `telegram`, `jobs`,
`campaigns`, `free_config`, `config_sources`, `config_quality`, `lifecycle`,
`messaging`, `referrals`, `partners`, `content`, `competitors`, `feedback`,
`analytics`, `experiments`, `segments`, `admin`, `api`. No Redis, no heavy
platforms — durable jobs run on PostgreSQL.

## Production Safety Model

![Deployment Isolation](docs/images/deployment-isolation.png)

- GrowthOS owns **only** `/opt/pv-growth`, its container, its network, its DB
  role, its two socat forwarders and its logs — nothing else.
- Deploy chain: **build off-server → docker load → preflight → migrate →
  health → smoke before/after → rollback if anything regresses.** The server
  never builds images.
- Every table writes through unique idempotency keys; double scheduler runs
  provably cannot duplicate a message, post or reward.
- Baseline audit artifacts (before/after smoke, firewall checksums):
  [`docs/deployment-audits/`](docs/deployment-audits/).

## Free Config Engine

![Free Config Pipeline](docs/images/free-config-pipeline.png)

Two independent pools: **public** (admin-approved GitHub/Telegram sources →
parse → validate → dedupe → score → limited TCP health-check → publish top
1–2, always labeled community-sourced) and **PV exclusive** (admin-configured
quota/validity/location/per-user limits with per-date overrides, individual
claims preferred). Staged filtering never opens thousands of connections from
this VPN host.

## Lifecycle Automation

![Lifecycle Flow](docs/images/lifecycle-flow.png)

Data-driven rules (`lifecycle_rules`) with delay, conditions, cooldown,
max-sends and hard **stop conditions** — a purchase kills every inappropriate
sales reminder. Strict template rendering: missing commercial facts refuse to
send; numbers are never invented.

## Referral / Affiliate

![Referral Lifecycle](docs/images/referral-lifecycle.png)

Rewards fire **only after settled qualifying purchases** — never for clicks.
Self-referral blocked, one referral per referred user, daily farming cap,
ledger-level dedupe (a reward can never pay twice), configurable milestones
(1/3/5/10). Partners get a parallel commission ledger (pending→approved) —
the existing reseller dashboard was not rebuilt.

## Analytics & Admin

SQL-native funnels, cohorts, source performance (immutable first-touch),
revenue/renewal summaries — every number reconciles with the event log.
Deterministic A/B experiments (hash-stable per user, no winners declared on
weak samples). Admin API (token-auth) manages campaigns, templates, rules,
sources, flags, partners, experiments and shows queue depth, failed jobs and
recent events.

## Deployment

Full runbook: [`DEPLOYMENT.md`](DEPLOYMENT.md) · operations:
[`docs/operations/RUNBOOK.md`](docs/operations/RUNBOOK.md). Quick verify:

```bash
curl -s http://127.0.0.1:8350/health   # on the server
python -m pytest                       # 79 tests anywhere
python scripts/dod_audit.py            # machine-verifiable DoD
```

## Security

No secrets in Git (scanned) · admin token constant-time auth · rate-limited
ingestion/webhooks · least-privilege DB roles (`pv_growth`, `pv_growth_ro`
SELECT-only) · secrets in root-owned `.env` · pip-audit in CI · forbidden
destructive commands documented in [`SECURITY.md`](SECURITY.md).

## Testing

79 tests: idempotency everywhere, scheduler double-run safety, referral
fraud, commercial-fact fabrication rejection, webhook flows, Mirza sync
(transition/limit/outage), PG+SQLite parity. CI additionally runs migrations
up/down on both engines and builds the image.

## Repository Structure

```
src/pv_growth/       23 modules (see ARCHITECTURE.md)
alembic/versions/    0001..0007 (tested up+down, sqlite+postgres)
tests/               79 tests
scripts/             preflight/smoke CLI · backup.sh · loadtest.py · dod_audit.py · sentinelx-cleanup.sh
docs/                images (7 diagrams) · operations/RUNBOOK.md · deployment-audits/ · report/ (Persian PDF)
.github/workflows/   ci.yml (lint · test matrix · migrations · pip-audit · docker)
```

## Known Limitations (honest, current)

1. Telegram bot/publishing/lifecycle messaging are **built and tested but
   inactive** — needs the owner to supply a dedicated bot token + channel IDs
   (never reusing X-UI's token). Webhook returns 503 until then.
2. Mirza sync is passive observation of invoices; SERVICE_EXPIRED/RENEWED
   events are not yet derivable from Mirza's schema (marked unavailable
   rather than fabricated).
3. Analytics cover what flows through events; Mirza history (7,491 invoices)
   backfills at 300/cycle, then forward-only.

## License

Proprietary — PV Network internal.
