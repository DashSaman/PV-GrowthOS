# Customer campaign entry implementation

Goal: Existing authorized business-email links show the current purchase guide immediately while retaining exact source attribution. This is a conversion-path correction, not proof of acquisition.

Owner request on25Oct: continue actual acquisition without another permission loop; existing prices/manual support and all unknown/caps remain protected.

Scope: four registered campaign registries only; valid source rows and31-day windows. Unknown/malformed/unregistered tokens retain general behavior. No new contacts, posts, grant, price edits, payment automation, or message replay.

- Add an offline real-webhook regression: existing campaign token currently shows general menu instead of live catalogue/payment/direct purchase; prove RED.
- Resolve registered tokens before free-audience checks; preserve BOT_STARTED/SOURCE_ATTRIBUTED and first touch; record user-requested purchase guidance idempotently. Reuse current guide and keyboard.
- Cover four campaigns, missing/expired/corrupt registries, missing source, stale catalogue, same update replay, and unchanged organic/free/post starts.
- Run focused tests, full available suite, Ruff and full patch review. Create attached PR, exact-head CI and exact-main CI artifact; install only via new reviewed journal with backup/env/protected checks and read-only runtime verification.
- Keep native polling responsible; no synthetic live bot request/order/message.
