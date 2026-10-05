# PV GrowthOS Content + Instagram Growth Engine Design

**Date:** 2026-10-05  
**Status:** Approved by owner  
**Scope:** Finish the existing Content Engine and add an autonomous Instagram acquisition/sales loop to the existing PV-GrowthOS modular monolith.

## Outcome

PV-GrowthOS must be able to plan, validate, schedule, publish, measure, and optimize content without a daily human operator. Instagram is a first-class channel alongside Telegram, and the optimization target is downstream `trial`/`purchase` conversion attributable to `@pvnetwork_bot`, not raw reach.

The system must not promise that every post reaches Explore; Instagram distribution is outside our control. It should maximize the chance of useful distribution by learning from measurable outcomes while remaining policy-safe and fact-safe.

## Production audit baseline

Read-only audit on `RoboT` (`91.107.240.235`) found:

- Production `pv-growth-app` is healthy on `127.0.0.1:8350` and runs image `pv-growth-app:3de525a`.
- Commits after `3de525a` contain docs/audit changes only; production application code is functionally current.
- Alembic is at `0007` with 28 application tables.
- Six feature flags are on; `CONTENT_ENGINE_ENABLED`, `PARTNER_ENABLED`, `COMPETITOR_WATCH_ENABLED`, and `EXPERIMENTS_ENABLED` are off.
- `content_items` is empty.
- Existing `content.publish_due()` only publishes Telegram channel posts and is not called by the scheduler or job runner.
- Admin API does not expose ContentItem management despite the module docstring saying it manages content.
- One historical failed lifecycle job remains (`lifecycle disabled or user blocked`); it is not an active service failure but makes the current aggregate failed-job count non-zero.
- Production source is image-only; `/opt/pv-growth` holds config/backups/audit directories rather than a Git checkout. Deployment must continue to build immutable images off-server.

## Architecture

Keep the existing modular monolith and PostgreSQL job queue. Do not add Redis, Celery, a new database, or a second service unless later profiling proves a need.

The flow is:

`Planner -> Fact/Safety Validator -> Media Renderer -> Channel Publisher -> Insights -> Growth Optimizer -> next Planner cycle`

Content CTA links carry an attributable social campaign code into `@pvnetwork_bot`; existing attribution/event data closes the loop from content to trial/payment.

### 1. Operational Content Engine

Turn the existing state machine into an actual production subsystem:

- Add admin list/create/validate/schedule/retry endpoints for content.
- Add durable `content.publish_due` and `content.plan` jobs and enqueue them periodically.
- Route publication through a `Publisher` interface rather than hard-coding Telegram.
- Preserve `draft -> validated -> scheduled -> published/failed`; retries restart from `failed -> draft` only when explicitly requested or when a retryable transport failure is recorded.
- Add idempotency at both content-item and remote-publication level so scheduler overlap cannot double-post.

### 2. Instagram channel

Add `instagram` as a channel with transport isolated from business logic.

- Use Meta's supported Instagram API only; no password automation, browser bot, private endpoint, or stealth scraping.
- The account must be converted once from Personal to a Professional account and authorized for the needed Meta permissions. This is an external prerequisite, not something the server should bypass.
- Support `reel`, `post`, and `story` publication types behind the same publisher contract.
- Persist remote container/media IDs, publish IDs, status, and error category for safe retries and reconciliation.
- Pull insights for owned published media and normalize them into GrowthOS metrics.

### 3. Planner and optimizer

Planner output is structured, not free-form side effects. A proposal contains:

- content kind and Instagram format;
- campaign/source code;
- hook/body/CTA intent;
- structured commercial facts;
- media template + render variables;
- experiment variant and scheduled time;
- deterministic dedupe key.

The first optimizer is intentionally explainable: score candidates from recent reach/engagement plus attributable bot starts, trials, and payments. Downstream conversion outranks reach. It may exploit top formats/hooks and reserve a bounded exploration share for new variants. Weak samples must not auto-declare winners; reuse the existing experiments semantics.

### 4. Trend discovery

Trend discovery is pluggable and policy-safe:

- own Instagram insights and winner history are always available once connected;
- approved public/official trend sources may contribute structured topics;
- unsupported Instagram scraping is forbidden;
- provider failure degrades to evergreen/winner-derived planning and never stops VPN sales/provisioning.

Trend inputs are suggestions only. They cannot override commercial facts or safety rules.

### 5. Commercial facts and PV offer

Prices, traffic, validity, discount, locations, quotas, availability, uptime, and user counts must never be invented by a text/media generator. They come from campaign/config records and pass the existing fact validator before scheduling.

Current business context (seed/reference, not a hard-coded permanent truth):

- Economy: 1,500 per GB.
- Tunnel: 5,000 per GB.
- Active location context: US, DE, FI, TR.
- Free test is available.
- CTA destination: `@pvnetwork_bot`.

Changing a business fact in its source record must be sufficient to change future generated content; code deployment must not be required.

### 6. Branded media

Media generation must be deterministic enough to test and cheap enough for this 4 GB server.

- Keep a small template-based brand renderer for static cards and short video/story assets.
- Keep optional AI copy/media generation behind provider interfaces; the core loop must still function with deterministic templates if no AI credential is configured.
- Store only generated assets and metadata required for publication/reconciliation, with retention limits.
- Never expose bot tokens or signed storage credentials in public media URLs.
- If Meta requires a public fetchable URL for a media type, use a configured `MediaStore` abstraction; do not change protected Apache/Mirza routes implicitly.

### 7. Attribution and measurement

Every Instagram item gets a campaign/source identifier usable by the existing `social_<campaign>` parser and bot deep-link flow. Store a mapping from GrowthOS content ID to Instagram media ID and campaign code.

Measure at minimum:

- publish success/failure;
- reach/impressions/plays where the API exposes them;
- engagement actions where exposed;
- bot starts/source attribution;
- trial created/connected;
- checkout/payment;
- revenue/renewal attribution already available in GrowthOS.

Optimization objective is lexicographic/weighted toward purchases and trials, then qualified bot starts, then engagement/reach as leading indicators.

### 8. Safety and failure isolation

- Instagram and content automation get dedicated feature flags; both default off in new environments.
- Publishing fails closed on missing credentials, invalid facts, unsupported media, or ambiguous remote status.
- No Instagram failure may stop Mirza sync, provisioning, lifecycle, Telegram polling, or the main scheduler loop.
- HTTP calls have explicit timeouts and bounded retries; retry only errors classified as safe/retryable.
- Logs redact access tokens and storage credentials.
- A kill switch can stop Instagram publishing within the existing feature-flag cache window.
- Protected services/network/routes remain unchanged unless a separately reviewed deployment step requires it.

## Data changes

Extend rather than replace `content_items`. Add fields needed for format/provider state and create focused tables for publication attempts/remote IDs and insight snapshots if keeping them on `content_items` would make reconciliation ambiguous.

Migrations must be additive and reversible. Existing Telegram content behavior and existing rows must remain valid.

## Configuration

Add environment settings for Instagram/Meta and optional media/AI providers. Secret values live only in `/opt/pv-growth/config/.env` (or a later dedicated secret store), never Git or logs. Configuration should distinguish account/page IDs, access token, API base/version, timeouts, media-store credentials, and enable flags.

## Rollout

1. Ship schema + operational Content Engine with publishing flags off.
2. Run migrations and tests; verify protected services and `/health` unchanged.
3. Complete the one-time Instagram Professional/Meta authorization and configure secrets.
4. Enable Instagram in dry-run/planning mode; verify generated items, facts, assets, and attribution links.
5. Canary one real publication, reconcile its remote ID and insights, and verify bot attribution.
6. Enable bounded automatic scheduling; expand formats only after each format has one verified E2E publication.

Rollback is flag-off first, then previous image. Schema changes remain backward-compatible for the previous image.

## Definition of done

- Existing Content Engine is actually wired to admin + durable jobs + scheduler.
- Telegram behavior remains compatible and idempotent.
- Instagram adapter has contract tests for create/upload/publish/status/insights and safe retry classification.
- Reels, posts, and stories have distinct validated workflows.
- Planner can produce safe scheduled content without a human operator.
- No fabricated commercial facts can be published.
- Each published Instagram item is attributable through bot start to trial/payment events.
- Optimizer consumes normalized Instagram + GrowthOS conversion metrics without declaring winners on weak samples.
- Missing Meta/media/AI credentials degrade safely and never affect core VPN systems.
- New migrations upgrade/downgrade cleanly; full existing tests plus new tests pass.
- Production canary passes before continuous Instagram publishing is enabled.

## External prerequisites / honest limits

- A Personal Instagram account cannot be treated as an already authorized publishing account. Owner must complete the one-time Professional/Meta setup required by the supported API.
- Meta controls feed/Explore distribution; no implementation can guarantee 100% Explore placement.
- Full photo publishing may require a public-fetchable media store depending on the current supported Meta upload flow. The adapter must model this explicitly rather than silently modifying protected web routes.

