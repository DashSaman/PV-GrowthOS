# Instagram Growth Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a feature-flagged, policy-safe Instagram channel that autonomously plans, publishes, measures, and optimizes PV Network content for attributable trials and purchases.

**Architecture:** Extend ContentItem with creative metadata and add publication/insight records for reconciliation. A Meta API adapter implements the publisher boundary from the Content Engine plan; a deterministic planner/media layer feeds it, insights normalize into GrowthOS metrics, and a conservative optimizer uses downstream attribution as the main objective.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2/Alembic, httpx, PostgreSQL, existing jobs/experiments/analytics, pytest; optional local media tooling kept isolated behind interfaces.

**Spec:** `docs/superpowers/specs/2026-10-05-instagram-growth-engine-design.md`

## Global Constraints

- Supported Meta Instagram API only; no password automation/private API/stealth scraping.
- Personal -> Professional/Meta authorization is a one-time external prerequisite.
- Do not promise Explore placement.
- Commercial facts come from campaign/config data and must pass validation.
- `@pvnetwork_bot` social deep-link attribution closes the optimization loop.
- New flags default off; Instagram failure must never affect Mirza/provisioning/lifecycle/Telegram.
- Production remains image-based; builds happen off-server and rollback is previous image + flags off.
- No protected Apache/Mirza route change is implicit in media hosting.

## Review Focus

- Meta returns success for container creation but publish status is ambiguous/timeouts: reconcile before retrying publish; pin in Task 3.
- Token/API error bodies can contain sensitive request data: logs must redact credentials and bound stored error text; pin in Task 2.
- Photo requires a public-fetchable URL while local media store is absent: fail closed as not configured, never publish a broken item; pin in Task 4.
- Planner runs twice for the same day/slot: deterministic dedupe prevents duplicate content; pin in Task 5.
- Insights are missing/renamed/partial or conversion sample is weak: normalize missing values to unavailable and never auto-declare a winner; pin in Task 6.

---

### Task 1: Instagram schema, configuration, and feature flag

**Files:**
- Create: `alembic/versions/0008_instagram_growth.py`
- Modify: `src/pv_growth/database/models/engagement.py`
- Modify: `src/pv_growth/database/models/__init__.py`
- Modify: `src/pv_growth/core/config.py`
- Modify: `src/pv_growth/core/flags.py`
- Create: `tests/test_instagram_schema.py`

**Interfaces:**
- Produces: `ContentPublication`, `ContentInsight`, new ContentItem `format` + `creative` fields, flag `INSTAGRAM_AUTOMATION_ENABLED`, and settings prefixed `instagram_*`/`media_*`.
- Required enable-time settings: professional account ID, access token, explicit API version; optional media-store/AI settings remain empty by default.

- [ ] **Step 1: Write failing schema/config tests**
  Assert new models/columns exist after migration, uniqueness prevents duplicate remote-publication identity, Instagram flag defaults off, and missing credential fields stay empty without leaking secrets.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest tests/test_instagram_schema.py -v`
  Expected: FAIL before migration/models exist.
- [ ] **Step 3: Implement additive `0008` + models/settings/flag**
  `ContentPublication` stores provider/status/container_id/media_id/error category/attempt count/timestamps; `ContentInsight` stores publication ID, captured time, and normalized JSON metrics. Downgrade drops only new tables/columns.
- [ ] **Step 4: Run schema tests and migration upgrade/downgrade/upgrade**
  Expected: PASS with head `0008` and clean round-trip on disposable DB.
- [ ] **Step 5: Commit**
  Commit: `feat(instagram): add growth-loop schema and config`

### Task 2: Meta Instagram API client with fake transport

**Files:**
- Create: `src/pv_growth/instagram/__init__.py`
- Create: `src/pv_growth/instagram/client.py`
- Create: `tests/test_instagram_client.py`

**Interfaces:**
- Consumes: Task 1 Instagram settings.
- Produces: `InstagramTransport.request(method, path, *, params=None, data=None, headers=None) -> dict`, `HttpInstagramTransport`, `FakeInstagramTransport`, and `InstagramClient` methods `create_container(...)`, `upload_video(...)`, `container_status(...)`, `publish_container(...)`, `media_insights(...)`.

- [ ] **Step 1: Write failing request/response contract tests**
  Cover post, reel, story parameters, direct/resumable video upload path, insight retrieval, explicit timeouts, missing config, 4xx non-retryable classification, 429/5xx retryable classification, and token redaction.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest tests/test_instagram_client.py -v`
  Expected: FAIL because client package does not exist.
- [ ] **Step 3: Implement the smallest synchronous httpx client**
  API version comes from config instead of a guessed hard-coded current version; raise typed `NotConfigured`/`ExternalServiceError` subclasses or result categories usable by the publisher.
- [ ] **Step 4: Run client tests**
  Expected: PASS without real network access.
- [ ] **Step 5: Commit**
  Commit: `feat(instagram): add supported-api client`

### Task 3: Reconciled Instagram publisher

**Files:**
- Create: `src/pv_growth/instagram/publisher.py`
- Modify: `src/pv_growth/content/publishers.py`
- Modify: `src/pv_growth/content/jobs.py`
- Create: `tests/test_instagram_publisher.py`

**Interfaces:**
- Consumes: Task 2 `InstagramClient`, Task 1 publication records, Content Engine `Publisher`.
- Produces: `InstagramPublisher.publish(item, rendered_body) -> PublishResult` and `reconcile(publication_id) -> ContentPublication`.

- [ ] **Step 1: Write failing state/retry tests**
  Cover happy path, remote container created then timeout, status reconciliation before retry, publish ID persistence, unsupported format, flag off, and one Instagram failure not affecting a subsequent Telegram item.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest tests/test_instagram_publisher.py -v`
  Expected: FAIL before publisher exists.
- [ ] **Step 3: Implement container/publication state machine**
  Persist remote IDs before later calls; ambiguous operations reconcile instead of blindly repeating; only safe/retryable failures advance attempt counters.
- [ ] **Step 4: Wire Instagram publisher into `content.publish_due` job only when both content and Instagram flags are on**
- [ ] **Step 5: Run Instagram + phase5/job tests**
  Expected: PASS.
- [ ] **Step 6: Commit**
  Commit: `feat(instagram): publish with remote reconciliation`

### Task 4: Deterministic branded media and media-store boundary

**Files:**
- Create: `src/pv_growth/content/media.py`
- Create: `src/pv_growth/content/media_store.py`
- Modify: `requirements.txt` and/or `Dockerfile` only if the chosen minimal renderer needs a dependency/binary.
- Create: `tests/test_content_media.py`

**Interfaces:**
- Produces: `RenderedAsset(path, mime_type, width, height, duration_seconds)`, `MediaRenderer.render(item) -> RenderedAsset`, and `MediaStore.put(asset, key) -> PublicAsset(url, expires_at)`.
- Instagram post requires a public-fetchable asset URL; video formats may use Task 2 upload when supported. Missing required store is `NotConfigured` and fail-closed.

- [ ] **Step 1: Write failing media tests**
  Assert 1:1 post and 9:16 reel/story outputs, deterministic render hash for identical input, bounded file size/duration, Persian/Latin text does not crash, cleanup/retention, and missing public store fails closed for photo publication.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest tests/test_content_media.py -v`
- [ ] **Step 3: Implement a resource-bounded template renderer and store interface**
  Keep AI media optional; deterministic templates are the no-credential fallback. Do not add a public server route or alter Apache in this task.
- [ ] **Step 4: Run media tests and inspect one generated fixture locally**
  Expected: formats/dimensions/size limits pass.
- [ ] **Step 5: Commit**
  Commit: `feat(content): add branded media pipeline`

### Task 5: Autonomous planner and attributable CTA

**Files:**
- Create: `src/pv_growth/content/planner.py`
- Create: `src/pv_growth/content/trends.py`
- Modify: `src/pv_growth/content/jobs.py`
- Modify: `src/pv_growth/jobs/scheduler.py`
- Create: `tests/test_content_planner.py`

**Interfaces:**
- Produces: `TrendSuggestion(topic, hook, source, observed_at)`, `TrendProvider.suggestions(...) -> list[TrendSuggestion]`, a no-network `PerformanceTrendProvider`, `plan_cycle(session, settings, *, now=None) -> list[ContentItem]`, and durable job `content.plan`.
- Inputs: active campaign config + planner AppConfig + previous content/insight scores; output CTA uses `https://t.me/pvnetwork_bot?start=social_<campaign>`.

- [ ] **Step 1: Write failing planner tests**
  Cover empty campaign data (no fabricated offers), exact fact copy from campaign, current PV price/location seed records treated as data not code, deterministic day/slot dedupe, reel/post/story mix, attributable CTA, repeated plan job idempotency, and trend-provider failure falling back to evergreen/performance-derived suggestions without changing commercial facts.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest tests/test_content_planner.py -v`
- [ ] **Step 3: Implement deterministic planning with evergreen fallback**
  No external trend/AI call is required for the core loop; `PerformanceTrendProvider` learns hooks/themes from owned winners. Additional approved trend providers plug into the same contract and may alter hook/theme only, never facts.
- [ ] **Step 4: Register/enqueue `content.plan` on a bounded cadence**
- [ ] **Step 5: Run planner/job tests**
  Expected: PASS.
- [ ] **Step 6: Commit**
  Commit: `feat(content): add autonomous social planner`

### Task 6: Insights, conversion scoring, and conservative optimization

**Files:**
- Create: `src/pv_growth/instagram/insights.py`
- Create: `src/pv_growth/content/optimizer.py`
- Modify: `src/pv_growth/content/jobs.py`
- Modify: `src/pv_growth/jobs/scheduler.py`
- Create: `tests/test_growth_optimizer.py`

**Interfaces:**
- Produces: `sync_insights(session, client, *, now=None) -> int`, `score_content(session, content_id) -> GrowthScore`, `rank_candidates(...)`, and durable job `instagram.insights_sync`.
- GrowthScore prioritizes attributable payment/trial/bot-start signals over engagement/reach and includes sample size/confidence metadata.

- [ ] **Step 1: Write failing insight/optimizer tests**
  Cover partial/missing Meta metrics, idempotent snapshot refresh, mapping content -> campaign/source -> bot/trial/payment events, purchase outranking reach-only content, weak sample refusing winner status, and Meta outage isolation.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest tests/test_growth_optimizer.py -v`
- [ ] **Step 3: Implement normalization and explainable scoring**
  Reuse existing analytics/experiments semantics where possible; store raw normalized metric dict for forward compatibility.
- [ ] **Step 4: Add bounded insights-sync scheduling**
- [ ] **Step 5: Run optimizer + analytics/experiment tests**
  Expected: PASS.
- [ ] **Step 6: Commit**
  Commit: `feat(instagram): close insights optimization loop`

### Task 7: Admin observability and dry-run controls

**Files:**
- Modify: `src/pv_growth/api/admin.py`
- Create: `tests/test_instagram_admin.py`

**Interfaces:**
- Produces authenticated endpoints to inspect Instagram readiness, publications, insight snapshots, planner dry-run output, and optimizer rankings. No endpoint returns tokens.

- [ ] **Step 1: Write failing admin tests**
  Assert auth, redaction, readiness reports missing prerequisites without secret values, publication/insight visibility, and dry-run creates no publication side effects.
- [ ] **Step 2: Run tests and verify failure**
  Run: `pytest tests/test_instagram_admin.py -v`
- [ ] **Step 3: Implement read/preview endpoints using service-layer functions**
- [ ] **Step 4: Run admin tests**
  Expected: PASS.
- [ ] **Step 5: Commit**
  Commit: `feat(admin): expose instagram growth controls`

### Task 8: Full verification and production-safe rollout package

**Files:**
- Modify: `DEPLOYMENT.md`, `ROLLBACK.md`, `STATUS.md`, `BLOCKERS.md`, `.env.example` if present.
- Add a deployment audit document after live rollout.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: immutable image candidate, migration/rollback commands, readiness checklist, one-canary procedure, and documented external prerequisites.

- [ ] **Step 1: Run full test/static/security-compatible suite**
  Run: `pytest -q`, `ruff check src tests`, `python -m compileall -q src`, `git diff --check`, and the repository's DoD audit.
  Expected: all local gates pass; any credential-only live checks report BLOCKED, not fake PASS.
- [ ] **Step 2: Verify `0007 -> 0008 -> 0007 -> 0008` on a disposable PostgreSQL database**
  Expected: clean round trip.
- [ ] **Step 3: Build the candidate image off-server and smoke it locally**
  Expected: `/health` ok and no secret baked into image history/environment defaults.
- [ ] **Step 4: Pre-deploy read-only production checks + database backup**
  Capture current image/container/network/health/protected-service baselines and create a verified `pv_growth` backup using the existing runbook.
- [ ] **Step 5: Deploy with both content/Instagram automation flags off**
  Migrate to `0008`, replace only `pv-growth-app`, keep binding/network/resource limits unchanged, then compare protected-service baselines.
- [ ] **Step 6: Configure one-time Meta/media credentials only after owner completes Professional/Meta authorization**
  Never echo credentials into logs/chat; readiness endpoint must become green before enabling.
- [ ] **Step 7: Dry run -> one canary -> insights -> attribution check**
  Enable a single bounded publication format, verify remote media ID + bot `social_<campaign>` attribution, then fetch insight snapshot.
- [ ] **Step 8: Expand automatic formats gradually**
  Enable reel/post/story only after each format has its own verified E2E result; leave kill-switch instructions recorded.
- [ ] **Step 9: Commit rollout docs/evidence**
  Commit: `docs(instagram): record safe growth-loop rollout`.
