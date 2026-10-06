"""Deterministic low-cost branded media rendering."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pv_growth.core.errors import NotConfigured, ValidationError
from pv_growth.database.models import ContentItem

POST_SIZE = (1080, 1080)
VERTICAL_SIZE = (1080, 1920)
VIDEO_SECONDS = 3


@dataclass(frozen=True)
class RenderedAsset:
    path: Path
    mime_type: str
    width: int
    height: int
    duration_seconds: float | None = None


class MediaRenderer:
    """Render a bounded brand card; no network or model credential required."""

    def __init__(self, root: str | Path, *, ffmpeg_path: str | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = ffmpeg_path or shutil.which("ffmpeg")

    @staticmethod
    def _fingerprint(item: ContentItem) -> str:
        payload = {
            "title": item.title,
            "body": item.body,
            "format": item.format,
            "facts": item.facts or {},
            "creative": item.creative or {},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:24]

    @staticmethod
    def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        )
        for candidate in candidates:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size=size)
        return ImageFont.load_default(size=max(10, size // 2))

    @staticmethod
    def _safe_color(value: object, fallback: str) -> str:
        if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
            try:
                int(value[1:], 16)
            except ValueError:
                return fallback
            return value
        return fallback

    def _frame(self, item: ContentItem, size: tuple[int, int]) -> Image.Image:
        creative = dict(item.creative or {})
        accent = self._safe_color(creative.get("accent"), "#7C3AED")
        image = Image.new("RGB", size, "#0B1020")
        draw = ImageDraw.Draw(image)
        width, height = size
        draw.rectangle((0, 0, width, max(18, height // 70)), fill=accent)
        draw.ellipse(
            (width * 0.62, height * 0.04, width * 1.10, height * 0.34),
            fill="#171D36",
        )
        title_font = self._font(max(38, width // 14))
        body_font = self._font(max(28, width // 24))
        label_font = self._font(max(22, width // 34))

        margin = width // 12
        y = int(height * 0.28)
        title = "\n".join(textwrap.wrap(item.title, width=22)[:3])
        body = "\n".join(textwrap.wrap(item.body, width=38)[:6])
        draw.multiline_text((margin, y), title, font=title_font, fill="#FFFFFF", spacing=16)
        y += int(height * 0.20)
        draw.multiline_text((margin, y), body, font=body_font, fill="#D8DCEF", spacing=12)
        draw.text((margin, height - margin * 2), "PV NETWORK", font=label_font, fill=accent)
        return image

    def render(self, item: ContentItem) -> RenderedAsset:
        if item.format not in {"post", "reel", "story"}:
            raise ValidationError(f"unsupported media format: {item.format}")
        digest = self._fingerprint(item)
        if item.format == "post":
            path = self.root / f"{digest}.jpg"
            if not path.exists():
                self._frame(item, POST_SIZE).save(path, "JPEG", quality=88, optimize=True, progressive=False)
            return RenderedAsset(path, "image/jpeg", *POST_SIZE)

        if not self._ffmpeg:
            raise NotConfigured("ffmpeg is required for Instagram reel/story rendering")
        path = self.root / f"{digest}.mp4"
        if not path.exists():
            frame_path = self.root / f"{digest}.frame.png"
            self._frame(item, VERTICAL_SIZE).save(frame_path, "PNG", optimize=True)
            command = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-i",
                str(frame_path),
                "-t",
                str(VIDEO_SECONDS),
                "-r",
                "12",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "-1",
                str(path),
            ]
            try:
                subprocess.run(command, check=True, timeout=25)  # noqa: S603
            except (subprocess.SubprocessError, OSError) as exc:
                path.unlink(missing_ok=True)
                raise ExternalMediaRenderError("ffmpeg could not render bounded MP4") from exc
            finally:
                frame_path.unlink(missing_ok=True)
        return RenderedAsset(path, "video/mp4", *VERTICAL_SIZE, float(VIDEO_SECONDS))

    def cleanup(self, *, retention_hours: int, now: datetime | None = None) -> list[Path]:
        if retention_hours < 1:
            raise ValidationError("media retention must be at least one hour")
        now = now or datetime.now(UTC)
        cutoff = now.timestamp() - (retention_hours * 3600)
        removed: list[Path] = []
        for path in self.root.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        return removed


class ExternalMediaRenderError(RuntimeError):
    """Local rendering failed without exposing command/environment details."""
