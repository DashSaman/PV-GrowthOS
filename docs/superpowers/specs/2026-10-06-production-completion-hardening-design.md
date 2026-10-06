# PV GrowthOS Production Completion + Correctness Hardening Design

**Date:** 2026-10-06
**Status:** Owner-approved on 2026-10-06
**Scope:** Close the correctness, retry, accounting, scaling, security, CI, and rollout gaps found in the 2026-10-06 whole-repository review, then ship the corrected repository and perform a protected production rollout on RoboT (`91.107.240.235`).

## Outcome

PV GrowthOS must be usable as an autonomous sales/growth system without silently losing settled payments, issuing unearned rewards, overstating commissions, dropping retryable customer actions, or learning from campaign-level signals as if they were content-level conversions.

Completion means the corrected code is merged to `main`, every local and CI gate is evidence-backed, a transferable immutable image exists without requiring a registry, and production is rolled out through the existing backup/preflight/smoke/canary contract. Protected PV services remain out of scope for mutation.

Instagram remains subject to Meta's external Professional-account/API authorization and a public media origin. Those prerequisites may block the Instagram canary, but they must not block core GrowthOS correctness or deployment of the corrected core with Instagram flags off.

## Non-negotiable safety constraints

- Work only in the PV-GrowthOS repository and GrowthOS-owned production resources.
- Mirza/MySQL remains read-only. X-UI may only be mutated through the existing GrowthOS provisioning boundary and only for `growth-*` service identities.
- Never modify PV reseller, AKH bot, Xray, tunnels, firewall rules, protected Apache routes, or unrelated Docker resources.
- Every customer-facing automation remains feature-flagged and fail-closed.
- Commercial values and financial rates must come from structured records. No fallback commission rate, price, quota, or availability number may be invented in code.
- All new side effects require durable idempotency. Ambiguous remote mutations are reconciled when proof exists and are never blindly replayed.
- Migrations are additive where possible, reversible, and tested on SQLite and PostgreSQL.

## Approach

Use incremental, backward-compatible hardening rather than a rewrite. The existing modular monolith, event log, PostgreSQL jobs, flags, and publisher boundaries stay in place. Fix the seams where the review found false completion or unsafe behavior, add only the schema necessary to represent missing state, and keep existing API contracts compatible unless they are demonstrably incorrect.

## 1. Mirza settlement state machine

The current seen-ID watermark is suitable for discovering new invoices but not for detecting an existing invoice changing from `unpaid` to `active`. Replace the implicit "seen means finished" assumption with explicit status-transition processing:

- Continue reading invoice IDs and a lightweight current status map from Mirza using SELECT only.
- Persist the last observed status per invoice in GrowthOS state.
- Fetch full invoice rows for both unseen invoice IDs and previously-seen IDs whose status changed into a state that requires new evidence, especially `unpaid -> active`.
- Emit `CHECKOUT_STARTED` once for an observed unpaid invoice and `PAYMENT_SUCCESS`/`SERVICE_CREATED` once when that same invoice later settles.
- Preserve the existing `active -> disabledn` evidence rule for `SERVICE_EXPIRED`.
- Event idempotency keys remain invoice-based, so replaying a transition cannot duplicate revenue or service events.
- Update the watermark only after the corresponding transition processing succeeds inside the GrowthOS transaction.

The regression test must demonstrate the exact previously-broken sequence: invoice `A` appears unpaid, a sync runs, invoice `A` becomes active, a second sync emits exactly one payment/service pair, and later syncs emit nothing new.

## 2. Settled-conversion coordinator

Add one GrowthOS-internal coordinator invoked only when a new settled `PAYMENT_SUCCESS` has been persisted. It owns downstream conversion effects so Mirza sync, event ingestion, referral, partner, and analytics do not each invent separate rules.

The coordinator receives `user_id`, an immutable `order_ref`, settled amount, and the newly-created payment event. It then:

- does nothing when the payment event is a replay (`created=False`);
- when `REFERRAL_ENABLED` is on, validates a pending referral for the buyer, rewards it exactly once, then checks newly reached milestones;
- when `PARTNER_ENABLED` is on and the buyer has an attributable partner source, records exactly one partner conversion using that partner's explicit commission policy;
- never blocks persistence of the settled payment if an optional growth effect is disabled or lacks required configuration; the missing/blocked effect is observable and retryable without fabricating values.

Referral reward semantics stay "settled purchase only". No click, bot start, checkout, or unpaid invoice can trigger a reward.

### Partner commission truth

`commission_entries` must distinguish gross order value from commission value. Add a `commission_cents` column while retaining `amount_cents` as gross order value for backward compatibility. `partner_summary()` sums `commission_cents`, never gross order value.

Automatic partner conversion requires an explicit structured commission policy on the Partner record (initially `meta.commission_bps`, integer basis points). There is no default percentage. If the policy is absent or invalid, automatic commission creation is refused and surfaced; an operator/API may still pass an explicit validated `commission_cents` to the existing service boundary. Existing entries are backfilled only when an unambiguous matching `PARTNER_CONVERSION` event contains their original `commission_cents`; otherwise they remain visibly unresolved rather than guessed.

## 3. Exclusive provisioning: correct quota + real retry

An exclusive claim is a durable reservation, not proof that a VPN service exists.

- Correct daily-budget accounting so the current claim does not consume its own pre-provision guard check. A budget of 1 must allow exactly one active/pending reservation, not zero.
- Make quota reservation concurrency-safe at the database boundary. Concurrent different users must not exceed campaign or daily limits.
- `pending_provision` must have an actual durable provisioning job with bounded exponential backoff.
- A retry uses the existing deterministic `claim_key -> growth-*` service identity, so a timeout/replay cannot create a second X-UI client.
- `FREE_CONFIG_CLAIMED` may represent the accepted reservation, but `TRIAL_CREATED` is emitted only after a real service/config has been confirmed active.
- Pending claims that exhaust retries become an explicit terminal failure state; they must not remain silently "queued" forever.
- X-UI delete/disable refuses any `service_ref` outside the `growth-*` namespace.

## 4. Lifecycle delivery semantics

Separate "message effect reserved" from "message definitely delivered".

- A message log row is created with an explicit delivery state and attempt count.
- Definite transport failures are retryable through the durable job queue.
- An ambiguous transport outcome is marked `delivery_unknown` and is not blindly resent, preventing spam/duplicates.
- `MESSAGE_SENT` is emitted only after a successful Telegram response.
- The lifecycle handler must not report `sent` when the underlying MessageLog is `failed` or `delivery_unknown`.

`max_sends` becomes operational rather than decorative. Lifecycle job and message dedupe identities include a stable send ordinal. Scan computes the next permitted ordinal from successful deliveries, respects cooldown and stop conditions, and can schedule another send until `max_sends` is reached. A purchase still stops inappropriate sales reminders before every send attempt.

## 5. Scheduler, lifecycle, job, and insight bounds

The single-container design stays, but periodic work must be bounded:

- Convert lifecycle candidate selection away from per-rule full-table N+1 scans toward set-based event queries.
- Recompute segments in bounded batches or only for users whose relevant state changed since the previous scan; never execute several queries per entire user population every scheduler tick.
- Use `settings.job_batch_size` and `settings.job_max_attempts_default` instead of hard-coded queue constants.
- Add a daily GrowthOS-owned job-retention task that removes completed/cancelled jobs older than a configured retention window while keeping failed jobs long enough for diagnosis. It never touches non-GrowthOS data.
- Instagram insight sync works on a bounded recent/eligible publication window and records snapshots on a configured cadence; old publications are not called forever every 15 minutes.
- Keep enough insight history for optimizer decisions while applying an explicit retention policy to obsolete snapshots.

These bounds must be configurable with conservative defaults compatible with the 384 MB / 0.75 CPU production limit.

## 6. Per-content Instagram attribution

Campaign attribution and content attribution are different facts. New planned Instagram items receive a compact, content-specific Telegram start token after the ContentItem has a database ID. The attribution parser resolves the token to:

- the exact `content_id`/source identity used by the optimizer; and
- the owning campaign, preserving campaign analytics.

Existing `social_<campaign>` links remain supported for backward compatibility but are treated as campaign-level evidence. The optimizer may use them for campaign reporting, but it must not assign the same campaign conversion count to every ContentItem and call that content-level evidence.

The planner generates unique tracked CTAs per item. A purchase attributed to item A must increase A's conversion score and not item B's, even when both belong to the same campaign.

## 7. Instagram ambiguous-publication reconciliation

`publish_unknown` remains fail-closed. The current mismatch where reconciliation expects a field the status request does not request must be removed.

The supported Instagram client gains an explicit reconciliation query based only on supported owned-media APIs. Reconciliation may mark a publication `published` only when it can identify exactly one owned media object using durable evidence tied to the ContentItem/publication (remote container relationship where supported, or a unique content marker/CTA plus bounded publication time when the API exposes only media-list data). Zero or multiple matches remain `publish_unknown`; `media_publish` is not blindly repeated.

The exact API fields/endpoint used at implementation time must be verified against current official Meta documentation and covered by transport contract tests. If the supported API offers no unambiguous lookup for a particular failure mode, the state remains safely unresolved rather than manufacturing certainty.

## 8. Security hardening

- An unset Telegram webhook secret disables the webhook route; there is no `dev` secret fallback. Compare configured secrets in constant time.
- X-UI TLS verification is enabled by default. A self-signed production panel requires an explicit GrowthOS setting to use a configured CA or, as a documented last resort, an explicit verification override. The risky choice must never be an implicit code default.
- Provisioning destructive calls enforce the `growth-*` namespace inside the adapter itself.
- Existing admin-token constant-time checks and localhost production binding remain unchanged.
- No credential is written to Git, logs, generated public media, CI artifacts, or audit reports.

## 9. CI and reproducibility

CI must fail when a required security/static gate fails:

- remove `|| true` from `pip-audit --strict`;
- make the chosen Ruff formatting policy consistent and blocking rather than reporting false green;
- keep SQLite + PostgreSQL tests and migration upgrade/downgrade coverage;
- keep the media-render dependencies installed in CI;
- create a deterministic dependency lock used by the production image while retaining human-maintained top-level requirements;
- produce an immutable, downloadable image artifact on successful `main` CI even when no external container registry secret exists.

`scripts/dod_audit.py` must execute or consume real evidence for security/CI requirements; the existence of a workflow line is not sufficient for PASS.

## 10. Deployment and rollback contract

Make the documentation and runnable commands describe the same production topology:

- canonical env file: `/opt/pv-growth/config/.env`;
- production container is an immutable image loaded/pulled by tag/sha, never built on RoboT;
- rollback stops/removes only `pv-growth-app`, selects the explicitly-known previous image, and does not depend on a compose project that did not create the live container;
- migration rollback uses the image that contains the matching Alembic migration code;
- backup is taken and verified before migration;
- protected-service smoke baselines are captured before and after replacement.

The CI image artifact closes the current no-registry distribution gap: download the tested artifact off GitHub, transfer it to RoboT, `docker load`, verify its image identity, then deploy. Do not build the candidate on the VPN host.

## 11. Production rollout sequence

1. Merge only after local tests/static checks, PostgreSQL migration round-trip, Docker build, and GitHub CI are green.
2. On RoboT perform read-only preflight and capture protected-service baseline.
3. Verify a GrowthOS database backup and record the currently-running image ID/tag and migration revision.
4. Load the tested immutable image artifact.
5. Force new/dangerous automation flags off, migrate GrowthOS only, replace only `pv-growth-app`, and verify `/ready` plus protected-service smoke.
6. Run core canaries: Mirza read-only sync reconciliation, lifecycle job health, referral/commission data integrity, and exclusive provisioning with only the existing GrowthOS-owned `growth-*` test identity where a real mutation is necessary and already authorized by the project contract.
7. Enable previously-live core flags gradually after canaries pass.
8. Instagram remains off until readiness proves Professional account ID, supported API token/version, and public media origin. If ready, run one planned item per format through attribution, publish, remote-ID reconciliation, insight sync, bot start, and conversion linkage before continuous scheduling.
9. On any protected-service regression, turn GrowthOS flags off and roll back GrowthOS immediately; never repair or restart the protected service as part of this rollout.

## 12. Verification and Definition of Done

Each defect gets a regression test that is observed failing before the implementation change and passing after it. At minimum the final suite covers:

- Mirza `unpaid -> active` payment transition exactly once;
- settled payment -> referral validation/reward/milestone exactly once;
- gross order amount distinct from partner commission and correct summary;
- daily free budget 1 permits exactly one claim; concurrent quota cannot oversubscribe;
- provisioning outage schedules a durable retry and eventual success emits `TRIAL_CREATED` once;
- Telegram definite failure retries; ambiguous delivery does not blind-resend; `max_sends > 1` sends the intended bounded count;
- lifecycle candidate/segment work remains bounded on a realistic synthetic population;
- old jobs/insights are pruned according to policy without deleting required failed/audit evidence;
- two Instagram items in one campaign do not share conversion counts;
- ambiguous Instagram publish reconciliation succeeds only on unique evidence and never re-publishes blindly;
- empty webhook secret cannot authenticate; X-UI destructive method rejects a paid/non-growth service reference;
- security/static CI gates return non-zero on a deliberately failing fixture/command;
- `0008 -> new head -> 0008 -> new head` works on SQLite and PostgreSQL;
- Docker image builds and health command succeeds;
- DoD audit has zero FAIL. Remaining BLOCKED items are only genuine external prerequisites and are documented with evidence.

No completion claim is made from the unit suite alone. The final evidence package includes local verification, GitHub CI job results, production pre/post smoke, migration revision, running image identity, feature-flag state, and the result of the production canaries.

## External blockers and usable completion

Core GrowthOS can and should be deployed independently of Instagram credentials, with Instagram flags off. Instagram is "usable" only after current supported Meta authorization and a Meta-fetchable public media origin exist and a real canary succeeds. The system must report these as readiness blockers rather than bypassing Meta, using private APIs, fabricating a successful state, or holding unrelated core fixes hostage.
