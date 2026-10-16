# Native growth research implementation plan

> **For agentic workers:** Execute directly using executing-plans. The owner has already authorized implementation without routine approvals.

**Goal:** Persist daily sourced business recommendations in Docker and expose them privately to the verified owner.

**Architecture:** A read-only external research job in the existing queue saves bounded reports in AppConfig. It has no publication or outreach handler. Existing creator authorization and native daily report provide management and delivery.

**Tech Stack:** Python, httpx, SQLAlchemy, existing Telegram client and scheduler; no new dependency or paid API.

**Spec:** docs/instagram-growth-20261016-fa.md

## Global Constraints

- FREE destination, quotas, protected services and historical effect reservations unchanged.
- Four fixed-host public directory requests/day, one MiB each, ten-second timeout, no redirects or retries after a source failure/challenge.
- Reports retain at most 30 daily records, bounded to 20 recommendations each. Baseline/list changes are not Instagram popularity.
- No password, cookie, token, personal demographics, automated cold message, or account grant.
- /manage authorization remains creator of at least one and administrator of both PV channels.

## Review Focus

- Missing/blocked/oversize/malformed source yields partial/unavailable, not zero businesses or success.
- Duplicate day and late job do not repeat external requests or overwrite historical reports.
- Unknown followers are not zero or verified below 20k.
- Public listing text cannot inject HTML into the private bot report or redirect a fetch to arbitrary hosts.
- Disabled research, unauthorized chat and release pause produce no external research effects.

## Task 1: Daily bounded research and honest ranking

Files: src/pv_growth/growth_research.py; tests/test_growth_research.py.
Interfaces: enqueue_daily(session, now); collect_daily(session, settings, payload, client=None); report_text(session, day=None); outreach_text(brand).

- [ ] Write and run failing tests for idempotency, four-request bound, failure classification, unknown followers, day rollover and disabled settings.
- [ ] Implement fixed-host directory adapter, explicit baseline/change evidence, deterministic recommendations and safe plain text.
- [ ] Verify matching tests and inspect full diff.

## Task 2: Native schedule and private owner controls

Files: jobs/scheduler.py, jobs/runner.py, telegram/management.py, config/automation-policy.json; tests/test_growth_research.py.

- [ ] Write and run failing tests for the daily enqueue, owner-only report and on/off control; assert no allocations/publications/messages to prospects.
- [ ] Register the handler, daily schedule and /manage read/on/off buttons; append saved digest to the existing native nightly report.
- [ ] Run focused and full project tests, lint/format, inspect full diff.
- [ ] Create/attach PR; exact-head CI green before merge. Deploy only exact green main artifact through a fresh bounded release journal and saved production procedure.
- [ ] Observe native report generation without forging an owner request or dispatching send jobs. Save checkpoint and real release evidence.
