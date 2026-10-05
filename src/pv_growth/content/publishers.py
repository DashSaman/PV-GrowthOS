"""Channel-specific content publishing adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pv_growth.core.config import Settings
from pv_growth.core.errors import NotConfigured, ValidationError
from pv_growth.database.models import ContentItem
from pv_growth.telegram.client import TelegramClient


@dataclass(frozen=True)
class PublishResult:
    remote_id: str
    metadata: dict


class Publisher(Protocol):
    def publish(self, item: ContentItem, rendered_body: str) -> PublishResult: ...


class TelegramPublisher:
    """Publish GrowthOS content to the configured Telegram channels."""

    def __init__(self, telegram: TelegramClient, settings: Settings) -> None:
        self._telegram = telegram
        self._settings = settings

    def publish(self, item: ContentItem, rendered_body: str) -> PublishResult:
        channels = {
            "free": self._settings.free_channel_id,
            "official": self._settings.official_channel_id,
        }
        if item.channel not in channels:
            raise ValidationError(f"unsupported telegram content channel: {item.channel}")
        channel = channels[item.channel]
        if not channel:
            raise NotConfigured(f"Telegram channel missing for content channel '{item.channel}'")
        result = self._telegram.send_channel_post(channel, rendered_body)
        return PublishResult(remote_id=str(result["message_id"]), metadata={"channel": str(channel)})
