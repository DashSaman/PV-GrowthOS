"""Public media-store boundary used by remote publishing APIs."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

from pv_growth.content.media import RenderedAsset
from pv_growth.core.config import Settings
from pv_growth.core.errors import NotConfigured, ValidationError


@dataclass(frozen=True)
class PublicAsset:
    url: str
    expires_at: datetime


class MediaStore(Protocol):
    def put(self, asset: RenderedAsset, key: str) -> PublicAsset: ...

    def cleanup(self, *, now: datetime | None = None) -> list[Path]: ...


class LocalMediaStore:
    """Copy assets to an operator-configured directory already exposed by a media origin.

    This class never configures that origin or modifies Apache/nginx routes itself.
    """

    def __init__(self, *, root: str | Path, public_base_url: str, retention_hours: int = 48) -> None:
        if not public_base_url.startswith(("https://", "http://")):
            raise ValidationError("public media base URL must be HTTP(S)")
        if retention_hours < 1:
            raise ValidationError("media retention must be at least one hour")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self.retention_hours = retention_hours

    @staticmethod
    def _key(key: str) -> PurePosixPath:
        parsed = PurePosixPath(key)
        if parsed.is_absolute() or not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts):
            raise ValidationError("invalid media store key")
        return parsed

    def put(self, asset: RenderedAsset, key: str) -> PublicAsset:
        parsed = self._key(key)
        target = self.root.joinpath(*parsed.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset.path, target)
        encoded = "/".join(quote(part, safe="-_.~") for part in parsed.parts)
        return PublicAsset(
            url=f"{self.public_base_url}/{encoded}",
            expires_at=datetime.now(UTC) + timedelta(hours=self.retention_hours),
        )

    def cleanup(self, *, now: datetime | None = None) -> list[Path]:
        now = now or datetime.now(UTC)
        cutoff = now.timestamp() - (self.retention_hours * 3600)
        removed: list[Path] = []
        for path in self.root.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        return removed


def build_media_store(settings: Settings) -> MediaStore:
    if not settings.media_public_base_url or not settings.media_store_root:
        raise NotConfigured("public media store is not configured")
    return LocalMediaStore(
        root=settings.media_store_root,
        public_base_url=settings.media_public_base_url,
        retention_hours=settings.media_retention_hours,
    )
