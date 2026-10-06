# Independent verified free growth review

Reviewed current files in `work/verified-growth` against exact base `6388743673beec4bec97fead1d44367b34ea502c` and the latest binding section of `docs/superpowers/specs/2026-10-06-verified-free-growth.md`. Snapshot has no complete Git metadata; changed-path inventory was supplied in `work/feature-changes.json`. Review was read-only; only this report was written. No servers, real APIs, bots, messages, commits, or repository state were mutated.

## Strengths

- Fresh Telegram membership plus complete, read-only Mirza history gates protect entry, winner provisioning, and private receipt. Positive matching private chat IDs are enforced. Unknown business statuses/history/backend state deny access.
- Main-channel sends are blocked at the Telegram client boundary; production community publication and old community receipt links are denied. Public owned cards hide URI and disclose shared quota.
- Exact byte reservations, separate 10/50 GiB buckets, global quota locks, frozen draws, one campaign win per user, deterministic growth-* identities, and durable pre-send receipt reservations are substantive protections. PostgreSQL concurrency regressions and migration 0012 are wired into CI.
- The isolated client pins an official Xray artifact/checksum, vets and pins global endpoint IPs, retains transport/TLS names, authenticates a localhost proxy, verifies actual HTTPS without direct fallback, and shuts down its process.
- Real panel state governs volume/time end; missing backend does not manufacture expiry. The new delivery loops store sanitized exception types and retain ambiguous reservations.
- Root's concurrent allocator fix is present: sparse entrant pools now receive randomized 50..1024 MiB quotas. The earlier all-1-GiB finding is resolved.

## Important findings

### P1 — Stale lottery work bypasses the actual daily grant cap

Files: `src/pv_growth/free_config/lottery.py:170` and `:317` (job input at `:438`). `freeze_draw` only rejects a draw before its cutoff, with no upper bound; `deliver_winner` accepts an allocation from any previous day and creates a fresh 24-hour service. After downtime/backlog, yesterday and today's allocations can all provision today while consuming different reservation-day buckets. This can exceed the 50 GiB and 100 winner limits on the actual provisioning day.

Confirmed in an isolated in-memory database: `freeze_draw(day=2026-10-05, now=2026-10-06 19:30)` created a winner, then `deliver_winner` returned True and created a service on October 6 against October 5's allocation. Existing shared-publish jobs already reject historical dates.

Fix: give lottery draws and first provisioning an explicit Tehran-day validity window; stop stale unprovisioned grants while retaining their reservations, or atomically account/re-reserve against the actual grant day before external creation. Check the day again immediately before create, including rollover while eligibility is checked. Add old draw, old pending delivery, and midnight rollover regressions.

### P1 — Delivered URI is not bound to the quota-proven panel client

Files: `src/pv_growth/free_config/owned_delivery.py:51` and `src/pv_growth/provisioning/xui.py:186`. Panel evidence verifies the deterministic client email/quota/expiry, but `_service_payload` discards authoritative client UUID and `verified_uri` accepts any healthy parseable URI from the configured HTTPS origin. There is no credential comparison tying the delivered URI to the client whose quota was checked. A misrouted/cached subscription response can deliver client B while proving client A, including an unrelated or paid service; a successful proxy request cannot prove this linkage.

Confirmed with a MockTransport response: a service carrying expected client identity `11111111-1111-4111-8111-111111111111` and the authorized subscription URL accepted a working VLESS URI with credential `22222222-2222-4222-8222-222222222222`. Current owned-subscription tests explicitly omit panel client identity, so they do not catch this.

Fix: preserve authoritative GET client identity and subscription ID, fail closed when missing or mismatched, require each URI's protocol credential to match that client before probing or delivering, and verify the subscription URL token matches the authoritative subId. Add a real-shaped panel GET to subscription-response integration regression for a matching UUID, different UUID, and missing identity. This finding does not assert that the live subscription endpoint currently returns the wrong account.

### P2 — Winner receipt can send after quota/expiry changes during the probe

File: `src/pv_growth/free_config/lottery.py:384`. Lottery calls `_panel_proof` once before subscription fetch/probe and fresh eligibility calls, then sends without rereading panel state. A retry near expiry or a service that exhausts quota during health testing can produce a receipt for an ended service. The shared private delivery path already rereads proof after health.

Confirmed in an isolated in-memory reproduction: the probe changed panel state to quota-exhausted and returned successful HTTPS; `deliver_winner` returned True, issued one message, and performed only one panel-state read.

Fix: call `_panel_proof` again after the actual probe and immediately before durable message reservation/send; block receipt on expired/exhausted/missing state. Add expiry/exhaustion-during-probe regressions. Public publication has the same ordering and should likewise recheck state before exposing a receipt button.

## Verification

Executed the existing targeted suite with temporary SQLite fixtures and no pytest cache: free budget, lottery, owned subscription, gated receipt, main-channel guard, verified expiry, trial end. Result: 53 passed, 2 PostgreSQL-only skips. Three isolated in-memory/MockTransport reproductions above confirmed the uncovered cases. Root reported the broader local suite and lint/format passing; this reviewer did not rerun the entire suite or Linux CI. PostgreSQL and pinned Docker binary behavior require the configured Linux CI result.

## Declined to judge

- Current live plan binding/canary and production rollout: parent owns those authorized real-server checks; this review intentionally made no live API or server mutations.
- Historical three main-bot canaries: explicitly accepted historical evidence, not a new batch or policy violation.
- Preventing someone from copying a URI after a legitimate grant: outside the membership delivery gate requirement.
- Iranian ISP performance, sales lift, competitor conversion claims: no evidence from these local tests; the implementation correctly avoids claiming them.
- Broad queue leasing, preexisting transport exception logging, and other existing architecture: not introduced by this diff. New draw checks can run up to 100 sequential users, so latency and 300-second job leases deserve attention in operations, but no duplicate external effect was proven from that timing alone.
- Mandatory end-CTA membership: required for receiving the service, not specified for a later purchase-only message; purchase exclusion is still rechecked.

## Assessment

Ready to merge: **with fixes**, after the three findings above receive regression coverage and Linux/PostgreSQL CI passes. The core design is careful, but date accounting and URI-to-client proof are material enforcement gaps.

# Follow-up review: one-day welcome gift and one-shot invitations

Scope: `welcome_gift.py`, dynamic gift budget cap and PostgreSQL SUM normalization, gift bot routing/membership context, gift expiry CTA extension, and `work/run_never_buyer_invites.py` plus its offline checks. No production state/API/message/commit changes were made by this reviewer.

The original three findings and subsequent shared private final-panel gate finding are fixed in the current files. Gift review found two additional one-day enforcement gaps, both now fixed during this review: `cap_for` enforces the fixed authorized 2026-10-06 day and <=1000 GiB maximum; the gift path rechecks the campaign after its final potentially slow audience/panel gate and immediately before provisioning. The final-gate midnight defect was reproduced before the fix, then covered by the focused suite.

## Strengths

- Each person has a lifetime gift reservation, an exact 100 MiB quota, an exact 24-hour panel expiry bound, durable pre-effect accounting, and private-only positive matching identity checks. Unknown deliveries are terminal and retries preserve budget.
- Existing original main-bot customer status and complete never-buyer/no-active history are read-only gates; both channel memberships, actual tunnel binding, authoritative client UUID/subId, exact panel quota/usage, and a real HTTPS probe precede receipt.
- The broadcaster uses a complete initial read-only snapshot plus fresh complete history immediately before each send, a verified existing main-bot private chat, unique durable one-shot campaign state and per-user receipt reservations, historical-canary exclusion, seven-day suppression, two workers, and a global two-dispatch-per-second clock. 429/401 stops the cohort and ambiguity is never replayed.
- Protected main-bot config checksum is verified before and after host execution; token remains in process memory; outputs avoid customer IDs, raw config URI, token and DSN. Main-channel/negative IDs cannot qualify as private recipients.
- Runner readiness now requires two distinct owned posts with initial health proof <=30 minutes old plus a bounded current panel identity/quota/expiry proof cache (at most two GETs per 30 seconds). This closes the earlier durable-only readiness gap.

## Remaining P2: user-level expiry CTA cooldown across gift and lottery

File: `src/pv_growth/provisioning/lifecycle_job.py:169` (claim-key dedupe and reservation). The new gift extension permits a user to have both a gift and a lottery trial, while end messaging only dedupes `trial_end:{claim_key}`. Two ended trials for the same eligible user therefore emit two purchase CTAs immediately; the binding conversion spec requires a cooldown.

Confirmed with an isolated in-memory database containing one expired lottery claim and one expired gift claim for the same user: two consecutive `trial_end_job` calls sent two messages. Existing tests verify per-claim replay only.

Fix: serialize expiry CTA reservation on the user row and suppress recent, reserved or unknown `pv_trial_end` receipts across both buckets, using an explicit cooldown. Retain per-claim dedupe. Add two-expired-trials/same-user regression, including a pending/unknown first attempt. This is a focused extension of the existing end-message guard, not a queue refactor.

## Current verification

Focused gift/gated/owned/CTA suite: **61 passed, 1 PostgreSQL-only skip**. Standalone broadcaster offline checks: **16 passed**. Root concurrently ran full/Linux/PostgreSQL CI; these are not independently asserted by this pass. Missing authoritative identity was independently confirmed to block. No live canary or broadcast was executed by this reviewer.

## Declined to judge

- Actual live owned canary, exact two live channel posts and deployment outcomes remain the parent agent's authorized production checks; no live requests were made here.
- Whether an invitation leads to joining/purchase or whether connection works on every Iranian ISP is outside this local verification; copy avoids those guarantees.
- Preventing re-sharing after a legitimate private URI receipt remains outside the bot delivery gate requirement.
- Broad modifications to old queue leasing/transport logging are not requested by this new scope.

Assessment: original findings closed; welcome-gift and broadcaster implementation is sound after the fixes above. Resolve the remaining user-level end-CTA cooldown before marking the expanded conversion flow fully complete; Linux/PostgreSQL CI and the bounded live canary remain necessary release evidence.

## Final follow-up closure

The remaining end-CTA cooldown finding is now fixed. `trial_end_job` locks the user row before reserving the receipt and suppresses any reserved/unknown expiry CTA plus sent expiry CTAs within 24 hours, across both lottery and gift claims. Per-claim replay protection remains in place. Independently reran the five end-message regressions: **5 passed**, including the new same-user gift/lottery cooldown test.

Final review assessment: **No remaining important findings in the reviewed code and one-shot runner scope.** Original findings, gift day/cap/final-gate findings, initial fresh-panel readiness, and shared private final-panel gate are closed. Approval is limited to code review; full Linux/PostgreSQL CI is still pending and the parent must complete the authorized bounded live canary before the broadcast. No production outcomes are inferred from offline tests.
