# PV GrowthOS Production Completion Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan. Every behavior change uses superpowers:test-driven-development; use superpowers:systematic-debugging for unexpected failures and superpowers:verification-before-completion before any completion claim.

**Goal:** Correct the reviewed production-safety, settlement, conversion, provisioning, lifecycle, Instagram, security, CI, and deployment defects; merge the evidence-backed result; then roll out only GrowthOS-owned resources on RoboT (`91.107.240.235`).

**Architecture:** Keep the existing modular monolith, event ledger, durable job queue, feature flags, and adapter boundaries. Add missing durable state only where correctness requires it, route settled payments through one idempotent conversion coordinator, make remote effects retry/reconcile explicitly, bound recurring work, and ship the exact CI-tested image to production.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, SQLite + PostgreSQL, httpx, Docker, GitHub Actions, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-06-production-completion-hardening-design.md`

## Global Constraints

- Touch only PV-GrowthOS and GrowthOS-owned production resources. Mirza/MySQL is SELECT-only.
- X-UI mutations are permitted only for deterministic `growth-*` identities. Never modify reseller/AKH/Xray/tunnels/firewall/protected Apache routes or unrelated containers.
- No guessed commercial values. Partner commission automation requires explicit structured policy.
- Every remote or financial effect is idempotent. Ambiguous remote writes are reconciled or left unknown, never blindly repeated.
- New/dangerous automation stays fail-closed until canary evidence exists.
- Migrations must round-trip `0008 -> head -> 0008 -> head` on SQLite and PostgreSQL before rollout.
- Do not commit local databases, credentials, generated secrets, production dumps, or media containing secrets.
- One task = tests observed RED, minimal implementation, focused GREEN, then a small commit. Keep unrelated existing changes untouched.

## Review Focus

Review every task for: payment-loss prevention, financial truth, concurrency/idempotency, retry-vs-ambiguous delivery semantics, bounded scheduler/database work, Meta API contract correctness, GrowthOS-only mutation boundaries, migration reversibility, fail-closed feature flags, and production rollback isolation.

---

## Task 1: Fix Mirza settlement transition detection

**Files**
- Modify: `src/pv_growth/mirza_adapter/mysql_reader.py`
- Modify: `tests/test_mirza_sync.py`

**Interfaces**
- Consumes the existing GrowthOS Mirza sync state and read-only invoice rows.
- Produces `CHECKOUT_STARTED`, `PAYMENT_SUCCESS`, `SERVICE_CREATED`, and `SERVICE_EXPIRED` through the existing idempotent event service.

1. Add a regression test where invoice `A` is `unpaid`, syncs once, becomes `active`, syncs again, and yields exactly one payment/service pair; a third sync must yield no new events.
2. Run `pytest tests/test_mirza_sync.py -k unpaid_to_paid_transition -vv` and record the current failure.
3. Refactor status discovery so work candidates include unseen invoices plus known invoices whose current status differs from persisted status; do not mark the new status durable until that transition's event writes succeed.
4. Preserve invoice-keyed event idempotency and the existing active-to-disabled expiry path. Do not introduce any MySQL write.
5. Run `pytest tests/test_mirza_sync.py tests/test_mirza.py -vv` and expect PASS.
6. Commit: `fix(mirza): process settlement status transitions`

## Task 2: Represent partner commission truth with a reversible migration

**Files**
- Create: `alembic/versions/0009_production_hardening.py`
- Modify: `src/pv_growth/database/models/growth.py`
- Modify: `src/pv_growth/partners/service.py`
- Modify: `tests/test_referrals.py`
- Create: `tests/test_migrations.py`

**Interfaces**
- `record_conversion(..., amount_cents, commission_cents)` stores gross and commission separately.
- `partner_summary()` sums only proven `commission_cents`.
- Migration `0009` owns only the partner accounting change; later durable-state tasks receive their own reversible revisions.

1. Write tests proving a 100000-cent order with 15000-cent commission reports gross `100000` and approved commission `15000`, and unresolved legacy commission is not silently treated as gross.
2. Run the focused partner test and observe the current wrong `100000` commission result.
3. Add nullable `commission_cents` to `CommissionEntry`; update service calculations to require/use it. In `0009`, backfill only rows with an unambiguous matching `PARTNER_CONVERSION` event containing validated `commission_cents`; leave other legacy values `NULL`/unresolved.
4. Add a migration regression that upgrades through `0009`, verifies the new column/backfill rule, downgrades to `0008`, and upgrades again. PostgreSQL round-trip is a final/CI gate in Task 12.
5. Test the SQLite migration round-trip locally.
6. Run `pytest tests/test_referrals.py -vv` and expect PASS.
7. Commit: `fix(partners): separate gross value from commission`

## Task 3: Route new settled payments through one conversion coordinator

**Files**
- Create: `src/pv_growth/conversions/__init__.py`
- Create: `src/pv_growth/conversions/service.py`
- Modify: `src/pv_growth/mirza_adapter/mysql_reader.py`
- Modify: `src/pv_growth/api/events.py`
- Modify: `src/pv_growth/jobs/runner.py`
- Modify: `src/pv_growth/referrals/service.py` only if a small idempotency boundary is missing
- Modify: `src/pv_growth/partners/service.py`
- Create: `tests/test_conversions.py`

**Interfaces**
- `enqueue_settled_conversion(session, *, payment_event, order_ref, amount_cents)` is called only when `PAYMENT_SUCCESS` was newly created.
- Durable job key is derived from the payment event idempotency key; replay returns the existing job/no-op.
- Worker validates referral and partner effects independently under their flags.

1. Write failing tests for settled payment -> referral validation/reward/milestones exactly once, replay no-op, partner source -> one conversion, and missing `meta.commission_bps` -> observable failed/blocked effect with no fabricated commission.
2. Run `pytest tests/test_conversions.py -vv` and record RED.
3. Implement the coordinator job. Resolve partner attribution from the buyer's existing source record; validate `commission_bps` as an integer basis-point policy and compute commission deterministically. Use event metadata `order_ref`/Mirza invoice id when available, otherwise the immutable payment event idempotency key.
4. Wire Mirza and public event ingestion to enqueue only on `created=True`. Payment persistence must commit even if an optional conversion effect later fails.
5. Register the handler in `jobs/runner.py`; keep referral/partner flags fail-closed.
6. Run `pytest tests/test_conversions.py tests/test_events.py tests/test_mirza_sync.py tests/test_referrals.py -vv` and expect PASS.
7. Commit: `feat(conversions): coordinate settled payment effects`

## Task 4: Make exclusive provisioning quota-safe and retryable

**Files**
- Create: `alembic/versions/0010_provisioning_retry.py`
- Modify: `src/pv_growth/database/models/free_config.py`
- Modify: `src/pv_growth/free_config/exclusive.py`
- Modify: `src/pv_growth/provisioning/guard.py`
- Modify: `src/pv_growth/provisioning/lifecycle_job.py`
- Modify: `src/pv_growth/jobs/runner.py`
- Modify: `tests/test_free_config.py`
- Modify: `tests/test_provisioning.py`

**Interfaces**
- A claim is first a durable quota reservation; successful provisioning upgrades it to active.
- `provisioning.claim` jobs retry the same `claim_key -> growth-*` identity with bounded attempts.
- `TRIAL_CREATED` is emitted only after the adapter confirms an active service/config.

1. Add RED tests: daily budget `1` allows one claim and rejects the second; concurrent distinct claimants cannot oversubscribe; provisioning failure leaves `pending_provision` plus one durable retry; eventual success activates once and emits one `TRIAL_CREATED`; exhausted attempts become `provision_failed`.
2. Run the focused tests and capture the existing budget=1 failure.
3. In reversible migration `0010`, add provisioning attempt/error state plus a date-scoped quota-lock table/row. Lock that row (`SELECT ... FOR UPDATE` on PostgreSQL) before count+insert. Keep same-user claim idempotency. Do not run the old guard in a way that counts the current reservation against itself.
4. Try provisioning once on the fast path. On a definite failure, persist pending state and enqueue `provisioning.claim`; retry handler increments attempt count and reuses the same deterministic service identity. Mark terminal failure after configured max attempts.
5. Emit `TRIAL_CREATED` only on confirmed activation; keep reservation evidence separate.
6. Run `pytest tests/test_free_config.py tests/test_provisioning.py tests/test_jobs.py -vv` and expect PASS on SQLite; concurrency behavior is repeated on PostgreSQL in Task 12.
7. Commit: `fix(provisioning): reserve quotas and retry claims safely`

## Task 5: Correct lifecycle delivery state and make `max_sends` real

**Files**
- Create: `alembic/versions/0011_lifecycle_delivery.py`
- Modify: `src/pv_growth/database/models/jobs.py`
- Modify: `src/pv_growth/telegram/client.py`
- Modify: `src/pv_growth/messaging/service.py`
- Modify: `src/pv_growth/lifecycle/service.py`
- Modify: `src/pv_growth/lifecycle/triggers.py`
- Modify: `tests/test_lifecycle.py`
- Modify: `tests/test_jobs.py`

**Interfaces**
- Message states distinguish reserved/sent/failed/delivery_unknown and record attempts plus a stable send ordinal.
- Definite retryable failures raise into the job queue; ambiguous outcomes become `delivery_unknown` and are not automatically resent.
- Dedupe keys include rule/user/ordinal/template version, so `max_sends > 1` is possible without duplicate sends for the same ordinal.

1. Add RED tests for: connection failure -> retryable failed job; read/remote ambiguity -> `delivery_unknown` with no resend; success -> `MESSAGE_SENT`; lifecycle result never says sent for failed/unknown; `max_sends=2` produces exactly two ordinals then stops; purchase stop-condition is rechecked before every send.
2. Run `pytest tests/test_lifecycle.py tests/test_jobs.py -vv` and record RED.
3. Introduce typed Telegram transport outcomes. Classify failures conservatively: safe pre-response/connect failures may retry; outcomes where Telegram may have accepted the request are ambiguous and must not blind-retry.
4. In reversible migration `0011`, add MessageLog attempt/send-ordinal delivery state and supporting indexes. Reserve or reuse a failed row per ordinal, increment attempts, set final delivery state from the actual response, and emit `MESSAGE_SENT` only after success.
5. Compute the next ordinal from sent/possibly-delivered effects; do not let a definite failed attempt consume a successful-send slot. Use actual template version in dedupe identity.
6. Run focused tests plus `tests/test_phase6.py`; expect PASS.
7. Commit: `fix(lifecycle): model delivery outcomes and send ordinals`

## Task 6: Bound scheduler, lifecycle, segments, jobs, and insights

**Files**
- Modify: `src/pv_growth/core/config.py`
- Modify: `src/pv_growth/jobs/scheduler.py`
- Modify: `src/pv_growth/jobs/runner.py`
- Modify: `src/pv_growth/jobs/service.py`
- Modify: `src/pv_growth/lifecycle/triggers.py`
- Modify: `src/pv_growth/segments/service.py`
- Modify: `src/pv_growth/instagram/insights.py`
- Modify: `tests/test_jobs.py`
- Modify: `tests/test_lifecycle.py`
- Create: `tests/test_instagram_insights.py`

**Interfaces**
- Runner defaults come from `job_batch_size` and `job_max_attempts_default`.
- Scheduler polling must not hold up periodic jobs.
- Retention and scan limits are explicit settings with conservative defaults.

1. Add RED tests showing configured job batch/max-attempt values are honored, completed/cancelled old jobs are pruned while failed diagnostic rows remain, lifecycle/segment query counts stay bounded for a synthetic population, and insight sync only selects the configured recent window/limit.
2. Add a scheduler timing/structure test proving Telegram long-poll does not block periodic job ticks.
3. Run `pytest tests/test_jobs.py tests/test_lifecycle.py tests/test_instagram_insights.py -vv` and record the unbounded/blocking RED cases.
4. Replace per-rule/per-user lifecycle N+1 candidate scans with set-based/subquery selection. Make bulk segment recomputation prefetch the needed facts in bounded queries/batches rather than invoking the per-user query path for all users.
5. Split long-poll execution from the scheduler tick path using the existing single-process boundary (one controlled polling worker/thread with clean shutdown); preserve one Telegram update pipeline.
6. Add GrowthOS-only job and insight retention functions and daily scheduling. Add explicit lookback/row-limit/retention settings; never prune failed jobs before the diagnostic retention rule.
7. Run the same focused command and expect PASS with query-count assertions.
8. Commit: `perf(jobs): bound recurring growth workloads`

## Task 7: Give every Instagram content item its own conversion attribution

**Files**
- Modify: `src/pv_growth/attribution/service.py`
- Modify: `src/pv_growth/content/planner.py`
- Modify: `src/pv_growth/content/optimizer.py`
- Modify: `tests/test_attribution.py`
- Modify: `tests/test_content_planner.py`
- Modify: `tests/test_growth_optimizer.py`

**Interfaces**
- New token format: compact `socialc_<content_id>`; persisted source code maps to exact content and its campaign.
- Legacy `social_<campaign>` remains valid campaign-level attribution but is never copied into every item score.

1. Write RED tests with two items in one campaign: planned CTAs differ; bot start through item A resolves A plus owning campaign; a payment from A raises A's downstream score and not B's; legacy campaign token still parses but does not become item-level evidence.
2. Run the three focused test modules and record RED.
3. After creating/flushing a ContentItem, generate its `socialc_<id>` CTA and persist the final body/creative metadata. Extend attribution resolution to validate the content id and retain campaign linkage.
4. Change optimizer attributed-count lookup from campaign source to the exact content source. Keep campaign-level analytics separately compatible with legacy links.
5. Run `pytest tests/test_attribution.py tests/test_content_planner.py tests/test_growth_optimizer.py -vv` and expect PASS.
6. Commit: `fix(instagram): attribute conversions per content item`

## Task 8: Reconcile ambiguous Instagram publishes with supported owned-media evidence

**Files**
- Modify: `src/pv_growth/instagram/client.py`
- Modify: `src/pv_growth/instagram/publisher.py`
- Modify: `tests/test_instagram_client.py`
- Modify: `tests/test_instagram_publisher.py`
- Modify: `docs/operations/RUNBOOK.md`

**Interfaces**
- Client exposes an owned-media reconciliation read that returns only documented fields.
- Publisher changes `publish_unknown -> published` only on exactly one durable match tied to the ContentItem and bounded publication time.

1. Before coding, verify the current endpoint and fields against official Meta documentation; record the official URL and chosen contract in the runbook. Do not use private/undocumented APIs.
2. Add RED transport tests for the verified request shape and publisher tests for exactly-one match, zero matches, multiple matches, and unsupported story/failure mode remaining unknown.
3. Run `pytest tests/test_instagram_client.py tests/test_instagram_publisher.py -vv` and record RED for the unsupported current reconciliation assumption.
4. Remove reliance on the nonexistent `published_media_id` field from container status. Match recent owned media using the unique content marker/CTA and a bounded timestamp only where the official API exposes enough proof.
5. Never call `media_publish` again from reconciliation. A zero/multi/unsupported result remains `publish_unknown` and observable.
6. Run the same focused command and expect PASS.
7. Commit: `fix(instagram): reconcile ambiguous publishes safely`

## Task 9: Harden webhook and X-UI security boundaries

**Files**
- Modify: `src/pv_growth/core/config.py`
- Modify: `src/pv_growth/api/webhooks.py`
- Modify: `src/pv_growth/provisioning/xui.py`
- Modify: `tests/test_phase6.py`
- Modify: `tests/test_provisioning.py`

**Interfaces**
- Empty webhook secret disables webhook authentication/processing; configured secret uses `hmac.compare_digest`.
- X-UI TLS verification defaults on; CA bundle or explicit override is configuration, not an implicit code default.
- `disable_service()` refuses any non-`growth-*` identity inside the adapter.

1. Add RED tests for unset webhook secret, configured secret compare, TLS default/configured CA/explicit override, non-growth destructive refusal, and exact-email matching in `_get_or_none`.
2. Run the focused tests and capture current fail-open behavior.
3. Implement the smallest security boundary changes. Never log secret values or Bearer tokens.
4. Run `pytest tests/test_phase6.py tests/test_provisioning.py -vv` plus `ruff check src tests`.
5. Commit: `security: fail closed at webhook and provisioning edges`

## Task 10: Enforce the documented A/B experiment contract

**Files**
- Modify: `src/pv_growth/experiments/service.py`
- Modify: `tests/test_phase6.py`

**Interfaces**
- Existing `split_percent` remains the A/B split between exactly two variants; the API no longer accepts variants that the assignment function can never select.

1. Add a RED test proving `create_experiment(... variants=["A", "B", "C"])` is rejected rather than silently creating an unreachable third arm.
2. Run `pytest tests/test_phase6.py -k experiment -vv` and record RED.
3. Change validation from “at least two” to “exactly two”; preserve deterministic assignment and existing two-arm records.
4. Run `pytest tests/test_phase6.py -vv` and expect PASS.
5. Commit: `fix(experiments): enforce two-arm assignment contract`

## Task 11: Make CI gates truthful and produce the deployable artifact

**Files**
- Modify: `.github/workflows/ci.yml`
- Modify: `requirements.txt`
- Create: `requirements.lock`
- Modify: `requirements-dev.txt` if lock tooling is needed there
- Modify: `Dockerfile`
- Modify: `scripts/dod_audit.py`
- Create: `tests/test_dod_audit.py`

**Interfaces**
- Human-edited top-level requirements remain readable; production image installs a deterministic generated lock.
- Main CI produces a downloadable immutable `pv-growth-app-<sha>` Docker image artifact even without registry credentials.
- Required format/security/audit commands fail CI when their underlying command fails.

1. Add tests/fixtures around DoD audit so a deliberately failed dependency/security evidence input returns non-zero instead of a false PASS.
2. Run `pytest tests/test_dod_audit.py -vv` and record RED against the current workflow-existence false positive.
3. Remove `|| true` from Ruff format and `pip-audit --strict`; make the audit consume actual command/CI evidence.
4. Generate and commit `requirements.lock` from `requirements.txt`; make CI verify the lock is in sync and make Docker install the lock. Audit the resolved lock, not an arbitrary environment. Do not pin secrets or environment-specific paths.
5. Make the Docker CI job tag the tested image by commit SHA, save it, checksum it, and upload it as a GitHub Actions artifact on successful main builds. Optional registry push may remain separate.
6. Run `ruff format src tests`, `ruff format --check src tests`, `ruff check src tests scripts`, `pytest tests/test_dod_audit.py -vv`, and a local `docker build` when Docker is available.
7. Commit: `ci: enforce gates and publish immutable image artifact`

## Task 12: Align deployment/rollback docs and run the complete pre-merge verification

**Files**
- Modify: `docker-compose.yml` only if retained for development/docs consistency
- Modify: `DEPLOYMENT.md`
- Modify: `ROLLBACK.md`
- Modify: `STATUS.md`
- Modify: `BLOCKERS.md`
- Modify: `README.md`
- Modify: `README.fa.md`
- Modify: older touched Instagram docs only to fix known whitespace/reporting inaccuracies; do not churn unrelated content.

**Interfaces**
- Canonical production env is `/opt/pv-growth/config/.env`.
- Deployment loads the CI artifact and replaces only `pv-growth-app`; rollback names an explicit previous image and matching migration code.

1. Make deployment, rollback, and compose references agree with the actual direct-container production topology. Remove any instruction to build on RoboT or `docker compose down` protected resources.
2. Update stale test/evidence counts only from fresh command output. Mark external Meta/public-origin blockers honestly; do not pre-claim them closed.
3. Run local full gates: `pytest -q`, `ruff check src tests scripts`, `ruff format --check src tests`, `python -m compileall -q src scripts`, `git diff --check`, and `python scripts/dod_audit.py`.
4. Run SQLite `0008 -> head -> 0008 -> head` migration round-trip and the PostgreSQL test/migration job. On PostgreSQL, rerun the quota concurrency test from Task 4.
5. Build/start the image locally, verify its health/ready endpoint, and record the image id/checksum. No completion claim until these commands are green.
6. Confirm `docs/images$name.png` has no repository references before removing it as reviewed orphaned debris; if any reference exists, keep it. Never infer orphan status from the filename alone.
7. Commit: `docs: align production rollout and rollback contract`

## Task 13: Review, merge, and perform the protected production rollout

**Repo/CI actions**
- Review only this branch's diff against current GitHub `main`; confirm no credential or unrelated-project file is present.
- Push `feat/instagram-growth`, open/update the PV-GrowthOS PR, wait for all required GitHub Actions jobs, and review the main-build Docker artifact/checksum.
- Use `superpowers:requesting-code-review` (or an equivalent independent review path if no reviewer agent is permitted), resolve only evidence-backed findings, then re-run verification.
- Merge only after green CI and approved review; record merged SHA and artifact identity.

**Production actions on RoboT (`91.107.240.235`)**
1. Read-only preflight first: running containers/image ids, GrowthOS migration revision, GrowthOS flag state, disk/memory, `/ready`, and protected-service smoke baseline. If ownership of a target is not unambiguously GrowthOS, stop before mutation.
2. Back up the GrowthOS PostgreSQL database, verify the dump is readable, and record the current `pv-growth-app` image id/tag for rollback. Do not copy credentials into logs/artifacts.
3. Download the exact successful GitHub Actions image artifact, verify checksum/SHA, transfer it to RoboT, and `docker load` it. Never build the candidate on the server.
4. Force new/dangerous GrowthOS flags off. Run Alembic upgrade using the candidate image and `/opt/pv-growth/config/.env`; verify the new revision.
5. Stop/remove and recreate **only** `pv-growth-app` with the known production limits/bind/mounts/network and immutable candidate tag. Do not restart/modify protected services.
6. Verify `/ready`, logs, database revision, feature flags, and the same protected-service smoke baseline. Any protected regression triggers GrowthOS flags-off + rollback of GrowthOS only.
7. Run core canaries: read-only Mirza reconciliation including a known settled-history sample, conversion/referral/commission integrity checks, lifecycle queue health, and one GrowthOS-owned `growth-*` provisioning canary only if the existing project contract authorizes that real mutation.
8. Re-enable only the previously-live core flags one at a time with health checks between changes.
9. Keep Instagram flags off unless readiness proves current Meta Professional-account authorization, supported API token/version, and public media origin. If all are ready, publish exactly one bounded canary per supported format and verify remote id/reconciliation, insights, `socialc_<content_id>` bot attribution, and conversion linkage before continuous scheduling.
10. Save the final evidence in `docs/deployment-audits/<UTC>-production-hardening-rollout.md`: merged SHA, CI URLs, image/checksum, migration revision, pre/post smoke, flag state, canary results, and any genuine external BLOCKED item. Never include secrets.

**Final verification**
- GitHub `main` contains the reviewed merge SHA and CI is green.
- Production `pv-growth-app` runs that exact artifact and `/ready` is healthy.
- Protected services match the preflight baseline.
- DoD audit has zero FAIL. Any remaining BLOCKED state is a real external prerequisite (for example Meta authorization/public media origin), not a code defect disguised as completion.

**Commit:** `docs: record production hardening rollout evidence`
