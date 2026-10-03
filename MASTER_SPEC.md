# MASTER_SPEC.md — PV GrowthOS v1.0

The authoritative specification is the approved "Autonomous Production-Safe
Implementation Specification v1.0" (owner-issued, 2026-10). This file is the
in-repo reference copy; in any conflict the owner spec wins. Summary of the
binding rules:

1. **Objective** — increase PV Network sales and long-term customer value with
   minimal manual operation. Every feature must serve the funnel: Acquisition →
   Bot Start → Free Config/Trial → Connection → Pricing → Checkout → Payment →
   Active → Renewal → Referral → Affiliate/Reseller → Win-back.
2. **Production safety** — live server; protected resources (Mirza, PV
   reseller, AKH bot, X-UI/Xray, tunnels, docker networks, iptables/nft) are
   read-only. With GrowthOS fully disabled, Mirza behavior must be unchanged.
3. **Namespace** — `/opt/pv-growth`, `127.0.0.1:8350`, `pv_growth_net`
   (172.23.77.0/24 if free), database `pv_growth` on existing PostgreSQL.
4. **Architecture** — modular monolith (FastAPI), PostgreSQL-backed durable
   jobs (no Redis in v1), no heavy platforms (PostHog/Mautic/n8n/Grafana are
   concept references only).
5. **Phases 0–7** strictly ordered, gated by acceptance criteria; PLAN.md is
   the single execution plan; anti-loop rules apply (3 attempts → document →
   change approach).
6. **AI/messages** — commercial facts (price, quota, uptime, counts) come only
   from structured data; no fake scarcity; rewards only after settled
   qualifying conversions; first-touch attribution is immutable.
7. **Resource limits** — ~250–400 MB RAM runtime, 384 MB container limit,
   0.5–0.75 CPU, bounded pids; no aggressive testing/crawling from this host.
8. **Deployment** — CI builds images; server pulls only. Deploy contract:
   preflight → backup → pull → migrate → start → health → smoke → or rollback.
9. **Definition of done** — machine-verifiable audit; no FAIL items; blockers
   only for genuinely human-only external dependencies (BLOCKERS.md).
