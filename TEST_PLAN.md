# TEST_PLAN.md

Test types per spec §39 and their mapping to acceptance criteria. Suite: pytest
(`python -m pytest`). CI runs the same suite against SQLite AND PostgreSQL 14.

| Suite | File | Acceptance covered | Status |
|---|---|---|---|
| Health/readiness | tests/test_health.py | P0: /health, /ready, 503-on-DB-down | ✅ |
| Preflight logic | tests/test_preflight.py | P0: port parsing, subnet overlap, route parsing | ✅ |
| Migration up/down | CI `Migration up/down test` job | P0: migrations work in test env | ✅ |
| Event ingestion idempotency | tests/test_events.py | P1: duplicate events idempotent | Phase 1 |
| Attribution persistence | tests/test_attribution.py | P1: first-touch immutable, last-touch updates | Phase 1 |
| Mirza adapter | tests/test_mirza.py | P1: read-only adapter contract | Phase 1 |
| Scheduler duplicate-safety | tests/test_jobs.py | P2/P3: no duplicate effects under double-run | Phase 3 |
| Config parsing/validation | tests/test_config_quality.py | P2: invalid configs never publish | Phase 2 |
| Exclusive quota/overrides | tests/test_free_config.py | P2: quota + per-date settings work | Phase 2 |
| Lifecycle stop-conditions | tests/test_lifecycle.py | P3: purchase stops reminders; cooldown; max sends | Phase 3 |
| Referral fraud | tests/test_referrals.py | P4: self-referral, duplicate reward, early reward | Phase 4 |
| Content fact validation | tests/test_content.py | P5: commercial facts cannot be fabricated | Phase 5 |
| Competitor isolation | tests/test_competitors.py | P5: watcher failure never propagates | Phase 5 |
| Analytics reconciliation | tests/test_analytics.py | P6: counts reconcile with events | Phase 6 |
| Experiment determinism | tests/test_experiments.py | P6: assignment stable per user | Phase 6 |
| Telegram mock flows | tests/test_telegram.py | P2/P3: deep-link, claim, message flows | Phase 2 |
| Protected-service smoke | src/pv_growth/smoke.py (CLI) | §39: before/after deploy | ✅ (script) |

Production smoke checklist (run before & after EVERY deploy): Mirza, PV reseller,
AKH bot, Apache, X-UI, PostgreSQL, GrowthOS — any failure after a GrowthOS
action triggers immediate GrowthOS rollback (never touching the protected service).
