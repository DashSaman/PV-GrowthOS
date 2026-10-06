# PV GrowthOS status

Last updated: 2026-10-06

## Release candidate

The production-hardening/Instagram-growth candidate is implemented through
Alembic `0011` on branch `feat/instagram-growth`. It is **not production-complete
until candidate GitHub CI is green, main produces the checksum-verified SHA
image artifact, and the protected rollout/post-deploy audit passes**.

Fresh local evidence for this candidate:

- `ruff format --check src tests`: 104 files formatted;
- `ruff check src tests scripts`: clean;
- `tests/test_dod_audit.py`: 3 passed;
- resolved production lock regenerated without drift;
- `pip-audit -r requirements.lock --strict`: no known vulnerabilities;
- SQLite migration rehearsal `0008 → 0011 → 0008 → 0011`: head `0011`;
- local Docker/PostgreSQL services are unavailable in the managed runtime, so
  image build plus full SQLite/PostgreSQL suite remain mandatory candidate CI
  evidence rather than being pre-claimed here.

The managed runtime also reproduces an AnyIO/Starlette cross-thread event-loop
hang for FastAPI TestClient; production behavior was not altered to hide that
harness limitation. Full ASGI coverage remains a required GitHub CI merge gate.

## What the candidate hardens

- Mirza payment state is transition-driven; repeated scans do not fabricate
  repeat settled conversions.
- Gross order value and partner commission are distinct; unresolved legacy
  commission remains unknown rather than being misreported as revenue.
- Settled conversion effects are coordinated through durable jobs and explicit
  policy; no fake reward/commission defaults.
- Exclusive provisioning has DB-backed quota serialization, deterministic
  GrowthOS-owned identities, bounded retry state and confirmed-activation
  `TRIAL_CREATED` semantics.
- Lifecycle delivery records reserved/sent/failed/unknown state and stable send
  ordinals; ambiguous Telegram transport is not blindly resent and `max_sends`
  is enforced.
- Queue, lifecycle, segments, Instagram insights and retention work are bounded.
- Instagram CTA attribution is per content item (`socialc_<content_id>`), so
  content optimization can connect a post to trial/purchase evidence instead
  of campaign-level vanity metrics.
- Ambiguous Instagram post/reel publishing reconciles conservatively against a
  unique owned-media marker; uncertain stories stay fail-closed.
- Telegram webhook has no dev-secret fallback. X-UI TLS verifies by default and
  destructive adapter operations reject non-`growth-*` identities before the
  network call.
- Experiments enforce exactly two variants.
- CI failures are no longer softened; production dependencies are hash-locked,
  and successful main builds export a SHA-addressed Docker image + checksum.

## Production baseline and rollout state

The last recorded production baseline (2026-10-05) was a healthy
`pv-growth-app` bound to `127.0.0.1:8350` on image `pv-growth-app:3de525a`,
Alembic `0007`, with the established core growth features operating and the new
Instagram automation off. This is historical evidence, not a claim that the
2026-10-06 candidate is already deployed.

Routine production deploy is direct-container only and uses
`/opt/pv-growth/config/.env`. The server must load the exact green main artifact;
it must not build the candidate itself. Rollout may replace only
`pv-growth-app` and migrate only the GrowthOS DB. Protected services are
read/smoke targets, never deployment targets.

## External Instagram gates

Instagram continuous automation intentionally remains off until both items are
real and readiness is green:

1. Meta-supported Professional account authorization/API credentials.
2. A public media origin that Meta can fetch and that maps to the configured
   GrowthOS media store.

These gates do not block deployment of the hardened core sales system. They do
block claiming that live Instagram publishing/Explore optimization is active.
See `BLOCKERS.md`.

## Business objective

The system's objective is increased sales, measured through attributable
bot-start → trial → payment → renewal evidence. Reach, likes and Explore
appearance are inputs, not the success criterion. Lifecycle, referrals,
exclusive trials, partners and Instagram content must therefore be judged by
downstream conversion/revenue evidence and safety stop conditions.
