"""Long-polling update source.

GrowthOS runs localhost-only (no public webhook URL), so we consume updates
via getUpdates long polling inside the scheduler thread — one mechanism only
(polling OR webhook), per the rollout spec. Update handling is shared with
the webhook path via api/webhooks logic.
"""

from __future__ import annotations

import httpx

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger

log = get_logger("poller")


class TelegramPoller:
    def __init__(self, settings: Settings, handler) -> None:
        self._settings = settings
        self._handler = handler  # callable(update_dict) -> None
        self._offset = 0

    def poll_once(self) -> int:
        token = self._settings.telegram_bot_token
        if not token:
            return 0
        try:
            resp = httpx.get(
                f"{self._settings.telegram_api_base.rstrip('/')}/bot{token}/getUpdates",
                params={
                    "timeout": 25,
                    "offset": self._offset + 1,
                    "allowed_updates": '["message","callback_query"]',
                },
                timeout=30.0,
            )
            body = resp.json()
        except httpx.HTTPError as exc:
            log.warning("getUpdates failed (isolated)", error=str(exc))
            return -1
        if not body.get("ok"):
            return -1
        updates = body.get("result", [])
        for update in updates:
            self._offset = max(self._offset, update["update_id"])
            try:
                self._handler(update)
            except Exception as exc:  # noqa: BLE001 — one bad update never stops polling
                log.error("update handling failed", update_id=update.get("update_id"), error=str(exc))
        return len(updates)
