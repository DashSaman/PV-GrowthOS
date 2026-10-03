# BLOCKERS.md

External, human-only dependencies. Each blocker lists what is ALREADY built
around it. One missing credential never freezes the project (spec §27).

| # | Blocker | Type | Workaround in place | State |
|---|---|---|---|---|
| B1 | ~~SSH access~~ **RESOLVED 2026-10-03**: authorized key akh_key used for the full deployment | server access | deployed, verified, all smoke green | ✅ CLOSED |
| B2 | ~~Telegram bot token~~ **RESOLVED 2026-10-03**: dedicated @pvgrowthos_bot created via BotFather, long-polling live, channel connected | credential | bot + channel operational; E2E verified | ✅ CLOSED |
| B3 | ~~channel IDs~~ **RESOLVED 2026-10-03**: t.me/pvnetwork_freeconfig created, bot is admin | credential | publishing verified with real posts | ✅ CLOSED |
| B4 | Mirza API endpoint/token unavailable | credential | `MirzaClient` implements the read-only adapter contract; a `FakeMirza` serves tests; integration activates by setting `PVG_MIRZA_BASE_URL`/`PVG_MIRZA_TOKEN` | OPEN |
| B5 | PV provisioning endpoint/token (exclusive pool) unavailable | credential | Claims queue safely as pending_provision (verified); no fabricated configs. X-UI panel credentials are human-only. | OPEN (only remaining runtime credential) |
| B6 | Container registry credentials (`DOCKER_REGISTRY` secret) unavailable | credential | CI builds (and tests) the image on every push; registry push step auto-enables when the secret exists | OPEN |

## Notes

- B1 subsumes: sentinelx-worker actual removal, on-host preflight execution,
  on-host smoke runs, production deploy. The scripts are final — running them
  is a 10-minute owner task following DEPLOYMENT.md.
- No blocker prevents any phase from being implemented and tested.
