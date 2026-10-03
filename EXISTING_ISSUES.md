# EXISTING_ISSUES.md

Pre-existing production conditions found during the earlier audit. These are
NOT GrowthOS problems and are NOT automatically GrowthOS's job to fix (spec §33).
Recorded here; only touched if they block GrowthOS with the smallest safe fix.

| Issue | Observed | Impact on GrowthOS | Action |
|---|---|---|---|
| `pv-gre.service` failed | systemd unit in failed state; GRE watchdog active and compensating | None (tunnels carry no GrowthOS traffic) | None — monitor only |
| X-UI Telegram `getUpdates` 409 conflicts | two pollers on one bot token | None (GrowthOS uses webhooks, never polling, on its own token) | None |
| Duplicated/legacy-looking Mirza cron entries | cron tree `/var/www/mirza_pro` | None | None — do not "clean" Mirza |
| Legacy server files | various leftovers in audit | None | None |
| No swap on 4 GB host | memory headroom is thin | GrowthOS keeps a hard 384 MB container limit; OOM risk noted in DEPLOYMENT.md | Monitor via `/health` + docker stats |

Rule: any new pre-existing finding goes in this table; GrowthOS work continues.
