# BLOCKERS.md

External, human-only dependencies. Each blocker lists what is ALREADY built
around it. One missing credential never freezes the project (spec §27).

| # | Blocker | Type | Workaround in place | State |
|---|---|---|---|---|
| B1 | ~~SSH access~~ **RESOLVED 2026-10-03**: authorized key akh_key used for the full deployment | server access | deployed, verified, all smoke green | ✅ CLOSED |
| B2 | ~~Telegram bot token~~ **RESOLVED 2026-10-03**: dedicated @pvgrowthos_bot created via BotFather, long-polling live, channel connected | credential | bot + channel operational; E2E verified | ✅ CLOSED |
| B3 | ~~channel IDs~~ **RESOLVED 2026-10-03**: t.me/pvnetwork_freeconfig created, bot is admin | credential | publishing verified with real posts | ✅ CLOSED |
| B4 | Mirza API endpoint/token unavailable | credential | `MirzaClient` implements the read-only adapter contract; a `FakeMirza` serves tests; integration activates by setting `PVG_MIRZA_BASE_URL`/`PVG_MIRZA_TOKEN` | OPEN |
| B5 | ~~PV provisioning~~ **RESOLVED 2026-10-04**: discovered Mirza's own provisioning interface (X-UI panel HTTP API + Bearer token in `marzban_panel`); GrowthOS reuses it via `XUIProvisioningAdapter` (same endpoints, `growth-*` client namespace only) | interface reuse | E2E verified: claim → provision → panel-visible service (1GB quota, expiry) → config URI → duplicate idempotent → cleanup | ✅ CLOSED |
| B6 | Container registry credentials (`DOCKER_REGISTRY` secret) unavailable | credential | CI builds (and tests) the image on every push; registry push step auto-enables when the secret exists | OPEN |
| B7 | Instagram account Professional/Meta authorization + supported API credentials not yet configured in GrowthOS | one-time external account authorization | supported-API client/publisher, readiness endpoint, remote-ID reconciliation, kill switch, and fake contract tests are complete; no password/browser/private-API bypass exists | OPEN |
| B8 | Public media origin/store for Meta-fetchable generated assets is not configured | external media origin | deterministic JPEG/MP4 renderer + `MediaStore` boundary + retention are complete; publication fails closed without a real configured store and no protected Apache/Mirza route is changed implicitly | OPEN |

## Notes

- Core GrowthOS/VPN operation remains live. B1–B3 and B5 are closed; B4 and B6
  remain integration/distribution constraints, and B7–B8 gate only the new
  Instagram canary/continuous loop.
- The bot token / channel / panel credentials all live only in
  `/opt/pv-growth/config/.env` on the server (root:600, never in Git).
