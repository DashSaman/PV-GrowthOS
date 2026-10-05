# Instagram Growth Loop — Pre-Deploy Audit — 2026-10-05T22:32Z

Scope: verify the new Content/Instagram candidate without changing protected
production services. This is deliberately a **pre-deploy** record; it does not
claim a Meta canary or production `0008` rollout occurred.

## Production baseline (read-only)

| Check | Observed |
|---|---|
| host | Ubuntu 22.04, RoboT / 91.107.240.235 |
| GrowthOS container | `pv-growth-app`, running + healthy |
| image | `pv-growth-app:3de525a` |
| network/IP | `pv_growth_net` / `172.23.77.2` |
| limits | 384 MiB · 0.75 CPU · 200 pids |
| `/health` | `status=ok`, app `pv-growth`, version `1.0.0` |
| Alembic | `0007` (unchanged) |
| Content Engine flag | false |
| Instagram flag | no DB override; code default is false |
| `akhbot-app` | up, healthy |
| `pv-reseller-dashboard` | up |
| `nine-router` | up |

The rollout is tracked in tmux session `instagram-rollout`. No firewall, tunnel,
Mirza/X-UI, Apache, reseller, AKH, or provisioning setting was modified.

## PostgreSQL migration proof

Because the local executor has no Docker/PostgreSQL runtime, the new `0008`
migration was mounted read-only into the **existing application image** and run
against a disposable PostgreSQL database on the production host. The live
`pv_growth` database was not used.

Observed sequence:

1. empty disposable DB → `0007`: PASS
2. `0007 → 0008`: PASS
3. `0008 → 0007`: PASS
4. `0007 → 0008`: PASS (`alembic_version=0008`)
5. disposable DB + temporary env/migration files removed: PASS

## Rollout state

- Local application/unit/integration tests: **160 PASS** after the final
  retry/kill-switch/retention review fixes.
- Static lint + compile + diff checks: PASS before final branch review.
- Definition-of-Done audit: **44 PASS · 0 FAIL · 9 BLOCKED**; every local
  requirement passes and the blocked checks require live access/credentials or
  a pushed candidate.
- Off-server immutable image build: **BLOCKED** in this executor (no Docker) and
  current repo registry distribution is B6. Production policy forbids replacing
  this with an on-server build.
- Production migration/replacement: **NOT RUN**; old healthy image and `0007`
  intentionally remain in place.
- Meta Professional/API authorization: **BLOCKED (B7)**.
- Public media origin/store: **BLOCKED (B8)**.
- Real Instagram dry-run/canary/insights: **BLOCKED by B7/B8 and flags remain OFF**.

This is the expected safe state: credentials-only live checks are BLOCKED, not
represented as fake PASS results.
