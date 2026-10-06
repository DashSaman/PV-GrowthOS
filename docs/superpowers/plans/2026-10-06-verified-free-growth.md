# Verified free acquisition implementation plan

> Use superpowers:executing-plans inline, TDD per task, and one independent final review.

Goal: verified working free configs plus budgeted daily shared PV and lottery acquisition.
Spec: 2026-10-06-verified-free-growth-design.md
Stack: Python3.12, SQLAlchemy/PostgreSQL, Alembic, existing Telegram/XUI interfaces, pinned Xray probe.

Global constraints: main channel forbidden; 45-day exclusion; 24h validity; 50 MiB–1 GiB private; 50 GiB private +10 GiB public maximum/day; growth-* owned resources only; existing384MB/.75CPU/200pids unchanged; all source safety guards preserved.

- [ ] Task1 protocol probe: tests RED; implement vetted outbound builder and bounded authenticated-localhost HTTPS probe; integrate production rank/recheck; pin official binary/checksum in Dockerfile; GREEN protocol and publication tests.
- [ ] Task2 quota/data: tests RED for sub-GiB exact bytes, replay mismatches, global allocation caps; migration0012 adds exact traffic bytes and lottery/public allocation records; implement XUI byte and real exhausted-state evidence; update migration CI assertion; GREEN/migration downgrade checks.
- [ ] Task3 public originals: tests RED destination/cap/dedupe/actual-health gating; implement10 spread public slots/day, exact1GiB24h deterministic service and subscription/probe; keep community flow bounded; GREEN.
- [ ] Task4 lottery: tests RED entry/repeat/buyer/closed-day/concurrent draw/quota; implement opt-in, cryptographic frozen-pool selection, locked durable reservations, provision+healthy private delivery, bypass prevention; GREEN.
- [ ] Task5 conversion: tests RED volume end/backend unavailable/purchase stop/single delivery; implement panel-backed sweep and end-of-trial CTA with cooldown/dedupe; GREEN related suite.
- [ ] Task6 integrate/release: independent review; fix important findings; full CI; attachPR, authorized merge; checksum immutable artifact; preflight/backup/migrate/rollout; protectedsmoke and no main config changes; activate verified production campaigns and controlled canary; save aggregate audit.
- [ ] Task7 measurements: retain existing bot/post6h24h baselines, mark new activation as a separate experiment; no causal sales claims; read-only heartbeat only, no unrequested new broadcasts.

Review focus: untrusted transport fields/DNS rebinding, exact quota zero/unlimited traps, reservation rollback after external effect, unavailable panel causing fake expiry, retry/deployment time crossing local midnight/draw windows.

Authorized execution: original user handoff explicitly authorizes code/tests/PR/CI/merge/deploy and forbids repetitive approvals; user supplied traffic economics and expiry. Execute without another permission menu.
