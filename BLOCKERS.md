# BLOCKERS.md

External, human-only dependencies. Each blocker lists what is ALREADY built
around it. One missing credential never freezes the project (spec §27).

| # | Blocker | Type | Workaround in place | State |
|---|---|---|---|---|
| B1 | No SSH access to the production Ubuntu server from the development machine | server access | Everything server-side ships as scripts/instructions: `preflight.py`, `smoke.py`, `scripts/sentinelx-cleanup.sh`, DEPLOYMENT.md runbook | OPEN — needs owner to run the runbook |
| B2 | Telegram bot token (`PVG_TELEGRAM_BOT_TOKEN`) unavailable | credential | Telegram client built against the Bot API with an injectable fake transport; all flows unit-tested with the fake; webhook endpoint refuses traffic when token missing | OPEN |
| B3 | Free-config / official channel IDs unavailable | credential | Channel targets are settings (`free_channel_id`, `official_channel_id`); publishing jobs validate target presence before send | OPEN |
| B4 | Mirza API endpoint/token unavailable | credential | `MirzaClient` implements the read-only adapter contract; a `FakeMirza` serves tests; integration activates by setting `PVG_MIRZA_BASE_URL`/`PVG_MIRZA_TOKEN` | OPEN |
| B5 | PV provisioning endpoint/token (exclusive pool) unavailable | credential | Provisioning client behind an interface + fake; exclusive campaigns queue claims until configured | OPEN |
| B6 | Container registry credentials (`DOCKER_REGISTRY` secret) unavailable | credential | CI builds (and tests) the image on every push; registry push step auto-enables when the secret exists | OPEN |

## Notes

- B1 subsumes: sentinelx-worker actual removal, on-host preflight execution,
  on-host smoke runs, production deploy. The scripts are final — running them
  is a 10-minute owner task following DEPLOYMENT.md.
- No blocker prevents any phase from being implemented and tested.
