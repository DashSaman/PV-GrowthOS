<div align="center">

# PV GrowthOS

**Autonomous, production-safe growth system for PV Network — designed, implemented, and running in production**

سیستم رشد خودکار و ایمن PV Network — طراحی‌شده، پیاده‌سازی‌شده و **در حال اجرا روی سرور اصلی**

[![CI](https://github.com/DashSaman/PV-GrowthOS/actions/workflows/ci.yml/badge.svg)](https://github.com/DashSaman/PV-GrowthOS/actions)
![Production](https://img.shields.io/badge/production-live%20%40%20RoboT-10b981)
![Tests](https://img.shields.io/badge/tests-CI%20gated-3b82f6)
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

## 🟢 Production baseline + release candidate (2026-10-06)

| Aspect | State |
|---|---|
| Deployed baseline | **YES** — `pv-growth-app` on host `RoboT`; last recorded baseline 2026-10-05 was healthy. The 2026-10-06 hardening candidate is not pre-claimed deployed |
| Binding | `127.0.0.1:8350` only (never 0.0.0.0) |
| Network | dedicated `pv_growth_net` (172.23.77.0/24 — collision-checked live) |
| Limits | 384 MB mem / 0.75 CPU / 200 pids |
| Database | production baseline: `pv_growth` on existing PostgreSQL, Alembic **0007**; candidate migration head is **0011** and has a tested downgrade chain |
| Mirza integration | established production path is read-only via `pv_growth_ro`; candidate makes payment ingestion transition-driven so repeat scans cannot fabricate settled conversions |
| Telegram | **LIVE**: dedicated bot [@pvgrowthos_bot](https://t.me/pvgrowthos_bot) + channel [PV Network | Free Config](https://t.me/pvnetwork_freeconfig) (bot = admin) · long-polling (localhost-only topology, one mechanism) · deep-link attribution `freecfg_*` verified end-to-end |
| Free Config / referral / lifecycle | established core paths remain the production growth loop; candidate adds quota serialization, bounded retry, truthful delivery state and settled-conversion coordination |
| **PV Exclusive provisioning** | existing XUI adapter is constrained to deterministic `growth-*` identities; candidate adds TLS verification by default and DB-backed quota/retry safety |
| Feature flags | 11 defined; customer-facing/dangerous automation defaults OFF and DB overrides remain the kill switch. Rollout restores only flags proven live before deploy |
| Protected services | Mirza 200→200 · Reseller 200→200 · AKH 401→401 · Apache/X-UI/Xray/Hedioum/tunnels: identical before/after (audited) |
| Candidate verification | local format/lint/DoD contract/security scan and SQLite `0008→0011→0008→0011` are green; full SQLite+PostgreSQL + Docker artifact is a mandatory GitHub CI merge gate |
| Release artifact | green `main` exports checksum-verified `pv-growth-app:<sha>` as downloadable Actions artifact; registry credentials are not required |
| Instagram activation | code is candidate-ready but live automation stays OFF until Meta Professional authorization + public media origin are real — see [BLOCKERS.md](BLOCKERS.md) |

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

## Instagram → sales attribution

Supported Meta publishing is isolated behind `CONTENT_ENGINE_ENABLED` and
`INSTAGRAM_AUTOMATION_ENABLED`. Every planned item carries a durable
`socialc_<content_id>` CTA marker, and bot attribution records the exact source
as `socialc:<content_id>` while retaining its campaign. This lets the optimizer
rank content with downstream trial/purchase evidence instead of treating reach
as revenue. Ambiguous post/reel publication is reconciled conservatively;
stories remain fail-closed when remote publication cannot be proven. Meta still
decides Explore/feed distribution — GrowthOS does not promise guaranteed
Explore placement.

## Deployment

Full runbook: [`DEPLOYMENT.md`](DEPLOYMENT.md) · operations:
[`docs/operations/RUNBOOK.md`](docs/operations/RUNBOOK.md). Quick verify:

```bash
curl -s http://127.0.0.1:8350/health   # on the server
python -m pytest                       # full local/CI suite
python scripts/dod_audit.py            # machine-verifiable DoD
```

## Security

No secrets in Git (scanned) · admin token constant-time auth · rate-limited
ingestion/webhooks · least-privilege DB roles (`pv_growth`, `pv_growth_ro`
SELECT-only) · secrets in root-owned `/opt/pv-growth/config/.env` · hash-locked
production dependencies + pip-audit in CI · forbidden
destructive commands documented in [`SECURITY.md`](SECURITY.md).

## Testing

Tests cover idempotency, scheduler safety, referral fraud, commercial-fact
fabrication rejection, webhook flows, Mirza transitions, provisioning
concurrency/retry, lifecycle delivery state, per-content attribution and
Instagram publish reconciliation. CI runs the full suite and migrations on
SQLite and PostgreSQL, audits the resolved lock, then builds the exact SHA image.

## Repository Structure

```
src/pv_growth/       23 modules (see ARCHITECTURE.md)
alembic/versions/    0001..0011 (reversible; SQLite+PostgreSQL CI)
tests/               unit, service, API, migration and integration-contract tests
scripts/             preflight/smoke CLI · backup.sh · loadtest.py · dod_audit.py · sentinelx-cleanup.sh
docs/                images (7 diagrams) · operations/RUNBOOK.md · deployment-audits/ · report/ (Persian PDF)
.github/workflows/   ci.yml (lint · test matrix · migrations · pip-audit · docker)
```

## Known Limitations (honest, current)

1. Live Instagram publishing is intentionally blocked until Meta Professional
   account authorization/API credentials and a Meta-fetchable public media
   origin are configured. No password/private-API workaround exists.
2. Explore placement is controlled by Meta and cannot be guaranteed. The
   system can automate supported publishing, exact attribution, insights and
   conversion-driven iteration.
3. Analytics report only evidence that reaches the event model; missing
   commercial facts remain unknown rather than being fabricated.

## License

Proprietary — PV Network internal.
