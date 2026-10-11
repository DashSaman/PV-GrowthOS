# Bot journey repair — 12 October 2026

The owner's report was reproducible: the bare bot start had no direct claim or lottery actions, `/free` and `/lottery` were ignored, and registration/denials were only transient callback notices. The transport itself was functioning: correct bot identity, polling enabled, no webhook conflict, administrator rights in both channels and recent processed starts.

The repair exposes current offers on the home menu and adds `/free`, `/web` and `/lottery`. Callbacks acknowledge before slow checks and keep registration, denial and unavailable responses in the private conversation. Registration explicitly means entry into the 23:00 Tehran draw, not immediate issuance.

The owner requested restoration of community configs from the web. They remain separately labelled third-party, with no PV ownership, quota or fixed-IP promise. Source downloads are capped at 1 MiB; production checks at most six candidates, with actual HTTPS through the proxy before publication and again before private delivery. The public post contains a gated bot link, not the raw URI. Both memberships, 45 days without purchase and no active/unknown paid service are still required. Durable private receipts and public reservations prevent uncertain sends from being blindly repeated.

The Tehran-midnight scheduler guard prevents yesterday's UTC hour from consuming the next day's slot 9. Ten owned one-GiB shared accounts per Tehran day and the separate 50-GiB lottery budget with 24-hour validity remain unchanged. Main-channel publication remains forbidden; the expired October 6 welcome gift remains off.

## Evidence and release gates

- Regression tests first reproduced the missing actions, ignored commands, transient results and midnight slot error, then passed after correction.
- Local full suite: 479 passed, 5 skipped; the Bash backup-script test is excluded on Windows and remains mandatory in Linux CI.
- Local Ruff check and format check passed. SQLite/PostgreSQL CI, Docker build, exact green main artifact and protected-service checks are still required before deployment.
- Live pre-repair observation: release `beb26044d5d8b8910cc27f28adf5b03eb772ba11`, healthy with protected smoke 7/7, 42 owned shared posts since October 8, two receipted lottery winners on October 8, free channel 7 members, initial invite batch 841 accepted and 101 forbidden receipts. No proven incremental sales lift.
- Read-only real web probe: Morpheus mini source had one of five candidates pass HTTPS (227 ms); Alirewa's first five did not pass. This is not a guarantee of future connectivity or Iranian ISP reachability. Runtime sources require fresh checks during activation.

## Business work still outstanding

Successful delivery is a prerequisite, not proof of growth. Competitor, partner, experiment and content runtime tables were empty in this audit. Instagram automation remains off pending supported authorization and a reachable media origin. Acquisition placements, attributable completed purchases and renewal effects still need real measurement. Native Mirza owns the separately approved 10% referral rule; GrowthOS must not duplicate wallet credit. Historical phase completion denotes implemented code, not activation or business success.
