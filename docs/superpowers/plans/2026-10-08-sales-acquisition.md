# Sales acquisition completion implementation plan

> Execute inline with superpowers:executing-plans; test behavior before implementation and obtain one independent whole-branch review.

Goal: turn the authorized healthy daily PV offers into an understandable, traceable acquisition and sales-followup flow. Complete the remaining existing MASTER_SPEC funnel without a proxy or unapproved rewards.

Spec: MASTER_SPEC.md, docs/superpowers/specs/2026-10-06-verified-free-growth.md, owner handoff and October 8 instruction to execute all goals. The owner explicitly authorizes code/tests/PR/review/merge/CI-artifact deployment and forbids repetitive approvals. Existing budget remains 10 shared GiB plus 50 lottery GiB/day; ten posts every two hours during active hours. No main-channel posts, protected changes, new gifts, reward defaults or external unsolicited outreach.

Architecture: existing FastAPI/PostgreSQL jobs and Telegram handlers; no extra platform, infrastructure or database. Production owns scheduling. Purchase-bot API use is confined to existing never-buyers, fresh complete SELECT history, private identity proof, prior-message exclusion and a durable reservation. Secrets remain in the canonical GrowthOS env, never code or DB.

## Task 1: public entry and voluntary sharing
- [ ] Tests RED: traced acquisition starts route to a current published shared offer; buyers keep purchase-only route; missing/expired/unverified offers do not grant; sharing contains no URI or personal data and no rewards; no main destination.
- [ ] Implement bounded acquisition copy/link helpers, latest-offer entry, /share and sharing buttons in the existing bot/public posts.
- [ ] GREEN focused Telegram, attribution, shared-delivery tests.

## Task 2: durable commercial followups
- [ ] Tests RED: receipt-based trial trigger, activation cutoff excludes historical events, fresh eligibility, global marketing cooldown, persisted reservation visible before network, process interruption cannot resend.
- [ ] Implement those conditions in the existing lifecycle/messaging flow. Existing confirmed panel expiry CTA remains active; never infer connectivity, paid expiry or price from imports.
- [ ] GREEN existing lifecycle/messaging tests. Production templates will be plain original Persian; only current free offer and fixed purchase destination, no fake facts.

## Task 3: daily discovery and one-time invitations
- [ ] Tests RED: bounded readonly Mirza discovery, inactive/agent/not-home/paid/unknown rejection, two currently valid owned public proofs, daily limit, all historical invite/403/unknown exclusions, exact purchase-bot identity, private chat and complete response proof, interruption dedupe and Tehran daily scheduling.
- [ ] Implement one native daily job at 09:45 Tehran, at most 100 previously uninvited eligible existing main-bot users/day and at most two Bot API calls/sec, no catch-up or retries. Recheck history and availability at dispatch. No gift/secret/URI in invitation.
- [ ] GREEN focused tests and PostgreSQL CI effect locking. Runtime policy OFF until immutable reviewed release and healthy owned-post proofs.

## Task 4: honest source-level commercial report and rollout
- [ ] Tests RED for source payments before touch, duplicate users, backfills, currency and fixed time window; add bounded aggregate reporting without exporting personal identifiers.
- [ ] Whole relevant suite, lint and security; one fresh whole-branch review. Fix important findings with regression tests.
- [ ] Create/attach PR and exact-head CI; authorized merge; immutable main artifact and complete checksum; protected preflight/backup/deploy/health/smoke with rollback evidence.
- [ ] Activate new bounded policies/templates, disable only named obsolete canary rules, configure the already-authorized purchase-bot secret in GrowthOS only. One free-channel pinned guide with durable effect receipt; update its title/description only after verifying identity. No main posts or repeated daily configs/invites.
- [ ] Verify live policy, receipt, scheduler, protection and actual baseline; update the existing read-only monitoring prompt for the new source/window report. Record real external dependencies (referral budget, gift renewal, Instagram official access, accepted external placements).

## Review focus
Fresh business history immediately before dispatch; token/URI redaction on HTTP failure; daily budget and global old-message exclusion; no retry after lost receipt/commit; latest shared offer must still be healthy when claimed; first-touch and observation time are not proof of incremental sales.
