"""Telegram Bot API client with injectable transport.

The transport is swappable so every flow is unit-testable without network
(BLOCKERS.md B2). Real transport is plain httpx against api.telegram.org.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from pv_growth.core.config import Settings
from pv_growth.core.errors import ExternalServiceError, NotConfigured
from pv_growth.core.logging import get_logger
from pv_growth.core.publication_policy import refuse_main_channel

log = get_logger("telegram")


class TelegramRetryableError(ExternalServiceError):
    """The request was not accepted by Telegram and is safe to retry."""


class TelegramDeliveryUnknownError(ExternalServiceError):
    """Telegram may have accepted the request; automatic resend is unsafe."""


@dataclass(frozen=True)
class InlineKeyboard:
    buttons: list[list[dict]]  # [[{text, url|callback_data}, ...], ...]

    def to_api(self) -> list[list[dict]]:
        return self.buttons


class TelegramTransport(Protocol):
    def call(self, method: str, payload: dict) -> dict: ...


class HttpTelegramTransport:
    def __init__(self, settings: Settings) -> None:
        if not settings.telegram_bot_token:
            raise NotConfigured("Telegram bot token missing (PVG_TELEGRAM_BOT_TOKEN)")
        self._client = httpx.Client(
            base_url=settings.telegram_api_base.rstrip("/"),
            timeout=15.0,
        )
        self._token = settings.telegram_bot_token

    def call(self, method: str, payload: dict) -> dict:
        try:
            resp = self._client.post(f"/bot{self._token}/{method}", json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            log.error("telegram connection failed", method=method, error=type(exc).__name__)
            raise TelegramRetryableError("telegram connection unavailable") from None
        except httpx.HTTPError as exc:
            log.error("telegram delivery outcome unknown", method=method, error=type(exc).__name__)
            raise TelegramDeliveryUnknownError("telegram delivery outcome unknown") from None
        if not body.get("ok"):
            raise ExternalServiceError(f"telegram api error: {body.get('description')}")
        return body["result"]


class FakeTelegramTransport:
    """Records every call; canned responses per method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.canned: dict[str, Any] = {}
        self.fail_methods: set[str] = set()

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if method in self.fail_methods:
            raise ExternalServiceError(f"telegram: simulated failure of {method}")
        if method in self.canned:
            return self.canned[method] if callable(self.canned[method]) else self.canned[method]
        return {"message_id": len(self.calls), "chat": payload.get("chat_id"), "date": 0}

    def sent_texts(self) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage"]


class TelegramClient:
    def __init__(self, transport: TelegramTransport, settings: Settings) -> None:
        self._t = transport
        self._s = settings

    def get_me(self) -> dict:
        return self._t.call("getMe", {})

    def get_chat_member(self, chat_id: int | str, user_id: int) -> dict:
        return self._t.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def send_message(self, chat_id: int | str, text: str, keyboard: InlineKeyboard | None = None) -> dict:
        refuse_main_channel(chat_id)
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard.to_api()}
        return self._t.call("sendMessage", payload)

    def edit_message_text(self, chat_id: int | str, message_id: int, text: str) -> dict:
        return self._t.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            },
        )

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict:
        return self._t.call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
            },
        )

    def send_channel_post(
        self, channel_id: int | str, text: str, keyboard: InlineKeyboard | None = None
    ) -> dict:
        refuse_main_channel(channel_id)
        payload: dict[str, Any] = {"chat_id": channel_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard.to_api()}
        return self._t.call("sendMessage", payload)  # channels accept sendMessage

    def set_webhook(self, url: str, secret_token: str) -> dict:
        return self._t.call("setWebhook", {"url": url, "secret_token": secret_token})


def get_telegram(settings: Settings) -> TelegramClient:
    return TelegramClient(HttpTelegramTransport(settings), settings)
