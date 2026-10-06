# External blockers and release gates

Only dependencies outside this repository belong here. A missing external
credential must not be replaced with guessed values, password automation or an
unsupported/private Instagram API.

| ID | Dependency | Impact | Safe state |
|---|---|---|---|
| B7 | Meta-supported Instagram Professional account authorization, account id, access token and an explicitly selected supported Graph API version | Blocks the real Instagram publish/insight canary and continuous Instagram automation | `INSTAGRAM_AUTOMATION_ENABLED=false`; supported API client/fakes/readiness are implemented |
| B8 | Public HTTPS media origin mapped to the configured GrowthOS `MediaStore`, fetchable by Meta | Blocks media publication because Meta cannot fetch local-only generated assets | Content can be previewed/rendered; remote publishing fails closed and no protected Apache/Mirza route is changed |

## Not external blockers

- Container-registry credentials are no longer required. Green `main` CI saves
  the exact `pv-growth-app:<sha>` image, its SHA-256 checksum and uploads them as
  artifact `pv-growth-app-<sha>`. Production loads that artifact; it never
  rebuilds the release.
- Mirza's HTTP API token is not required for the established production
  read-only MySQL adapter path. The production credential remains the
  GrowthOS-owned `pv_growth_ro` SELECT-only account.
- X-UI provisioning uses only deterministic `growth-*` client identities. TLS
  verification is on by default; any real canary is explicit opt-in and must be
  cleaned up after verification.

## Candidate release gates

These are engineering gates, not external blockers and therefore cannot be
waived as "blocked":

1. Full GitHub CI on the candidate, including SQLite + PostgreSQL and the
   PostgreSQL quota-concurrency test.
2. Green `main` Docker job and checksum-verified SHA artifact.
3. Production preflight, GrowthOS-only DB backup, flags-off migration to `0011`,
   `/health` + `/ready`, and protected-service smoke before/after.
4. Post-rollout live audit. The provisioning create/verify/cleanup canary runs
   only with the audit's separate explicit mutation flag.

B7/B8 gate Instagram activation only; they do not prevent shipping the hardened
core funnel, lifecycle, referral, attribution and provisioning behavior.
