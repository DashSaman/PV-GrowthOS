# Content Engine Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the already-built Content Engine an actual production subsystem by wiring publisher abstraction, durable jobs/scheduler, and admin management while preserving Telegram behavior.

**Architecture:** Keep the existing ContentItem state machine and PostgreSQL job queue. Move channel-specific send logic behind a publisher contract, register a content job handler, enqueue it from the existing scheduler, and expose the state-machine operations through the authenticated admin API.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL/SQLite tests, existing Telegram client, pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-instagram-growth-engine-design.md`

## Global Constraints

- Keep the modular monolith; no Redis/Celery/new service.
- Do not change protected Mirza/X-UI/Apache/Hedioum/tunnel behavior.
- Commercial facts must pass the existing `validate_facts()` rules before scheduling.
- Existing Telegram publishing remains idempotent and backward-compatible.
- Customer-facing automation stays feature-flagged and fails closed.

## Review Focus

- Duplicate scheduler ticks: one due item must publish at most once; pin in Task 2.
- Unknown/unsupported content channel: fail the item without escaping the scheduler loop; pin in Task 1.
- Missing Telegram channel ID/token: fail closed with a categorized item failure; pin in Task 1.
- Admin illegal transition or fabricated fact: return a client error and persist no illegal state; pin in Task 3.
- Publisher exception during a batch: one bad item must not suppress later due items; pin in Task 1.

---

### Task 1: Channel publisher boundary

**Files:**
- Create: `src/pv_growth/content/publishers.py`
- Modify: `src/pv_growth/content/service.py`
- Modify: `tests/test_phase5.py`

**Interfaces:**
- Consumes: `ContentItem`, `Settings`, `TelegramClient`.
- Produces: `PublishResult(remote_id: str, metadata: dict)`, `Publisher.publish(item: ContentItem, rendered_body: str) -> PublishResult`, `TelegramPublisher`, and `publish_due(..., publishers: Mapping[str, Publisher]) -> int`.

- [ ] **Step 1: Write failing publisher-contract tests**
  Add tests proving Telegram publication returns a remote ID, unsupported channels fail only that item, a transport exception does not block the next due item, and two calls do not duplicate a successful send.
- [ ] **Step 2: Run the focused tests and verify failure**
  Run: `pytest tests/test_phase5.py -k 'publish_due or publisher' -v`
  Expected: FAIL because the publisher contract/mapping does not exist.
- [ ] **Step 3: Implement the publisher contract and refactor `publish_due`**
  Keep message rendering and fact/state rules in `content.service`; keep channel transport and channel-ID selection in `content.publishers`.
- [ ] **Step 4: Run focused tests**
  Run: `pytest tests/test_phase5.py -k 'content or publish' -v`
  Expected: PASS.
- [ ] **Step 5: Commit**
  Commit: `refactor(content): add channel publisher boundary`

### Task 2: Durable content publication job and scheduler wiring

**Files:**
- Create: `src/pv_growth/content/jobs.py`
- Modify: `src/pv_growth/jobs/runner.py`
- Modify: `src/pv_growth/jobs/scheduler.py`
- Modify: `tests/test_jobs.py`
- Modify: `tests/test_phase5.py`

**Interfaces:**
- Consumes: Task 1 `publish_due`, `get_telegram(settings)`, `jobs.enqueue`.
- Produces: registered handler `content.publish_due`, scheduler dedupe key `content_publish:<bucket>`, and `run_publish_job(session, settings, payload) -> None`.

- [ ] **Step 1: Write failing job-registration/scheduler tests**
  Assert built-in handler registration knows `content.publish_due`, periodic enqueue emits one deduped content job per interval, and overlapping scheduler calls cannot produce duplicate job rows.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest tests/test_jobs.py tests/test_phase5.py -k 'content and (job or scheduler)' -v`
  Expected: FAIL because no content job is registered/enqueued.
- [ ] **Step 3: Add the handler and scheduler enqueue**
  Handler builds only the configured Telegram publisher for now; publisher/credential failure is contained within content processing and must not escape the scheduler loop.
- [ ] **Step 4: Run job/content tests**
  Run: `pytest tests/test_jobs.py tests/test_phase5.py -v`
  Expected: PASS.
- [ ] **Step 5: Commit**
  Commit: `feat(content): wire durable publication job`

### Task 3: Authenticated admin ContentItem workflow

**Files:**
- Modify: `src/pv_growth/api/admin.py`
- Create: `tests/test_content_admin.py`

**Interfaces:**
- Consumes: existing `content.transition`, `content.schedule`, authenticated admin router.
- Produces: `GET /admin/api/content`, `POST /admin/api/content`, `POST /admin/api/content/{id}/validate`, `POST /admin/api/content/{id}/schedule`, `POST /admin/api/content/{id}/retry`.

- [ ] **Step 1: Write failing API tests**
  Cover auth, create/list, validation rejecting contradictory facts, schedule requiring an ISO timestamp, retry allowing only `failed -> draft`, and 404 for missing IDs.
- [ ] **Step 2: Run tests and verify route failures**
  Run: `pytest tests/test_content_admin.py -v`
  Expected: FAIL/404 because routes do not exist.
- [ ] **Step 3: Implement minimal request models and routes**
  Routes call the service state machine rather than reimplementing transition logic; convert `ValidationError` to HTTP 422.
- [ ] **Step 4: Run admin + phase tests**
  Run: `pytest tests/test_content_admin.py tests/test_phase5.py tests/test_phase6.py -v`
  Expected: PASS.
- [ ] **Step 5: Commit**
  Commit: `feat(admin): expose content workflow`

### Task 4: Regression verification for operational Content Engine

**Files:**
- Modify only if a regression test exposes a defect.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: a green baseline ready for the Instagram plan.

- [ ] **Step 1: Run all tests**
  Run: `pytest -q`
  Expected: all tests pass.
- [ ] **Step 2: Run static checks**
  Run: `ruff check src tests`
  Run: `python -m compileall -q src`
  Run: `git diff --check`
  Expected: all exit 0.
- [ ] **Step 3: Run migration round-trip in the project's supported test path**
  Run the existing Alembic upgrade/downgrade CI procedure against the disposable test database; expected head remains `0007` for this plan.
- [ ] **Step 4: Commit any test-only fix discovered during verification**
  Commit only if required: `test(content): close operational regressions`.

