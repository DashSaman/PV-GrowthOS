"""Deterministic, bounded media-pipeline tests."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest

from pv_growth.core.errors import NotConfigured
from pv_growth.database.models import ContentItem


def _item(**updates):
    values = {
        "title": "PV Network",
        "kind": "education",
        "channel": "instagram",
        "format": "post",
        "body": "اینترنت آزاد — PV Network / Fast & Simple",
        "facts": {},
        "creative": {"template": "brand_card", "accent": "#7C3AED"},
    }
    values.update(updates)
    return ContentItem(**values)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_post_renderer_is_square_deterministic_and_bounded(tmp_path):
    from pv_growth.content.media import MediaRenderer

    renderer = MediaRenderer(tmp_path)
    first = renderer.render(_item())
    second = renderer.render(_item())

    assert (first.width, first.height) == (1080, 1080)
    assert first.mime_type == "image/jpeg"
    assert first.duration_seconds is None
    assert first.path.stat().st_size < 2_000_000
    assert _sha256(first.path) == _sha256(second.path)


@pytest.mark.parametrize("format", ["reel", "story"])
def test_vertical_video_is_9_by_16_short_and_bounded(tmp_path, format):
    from pv_growth.content.media import MediaRenderer

    asset = MediaRenderer(tmp_path).render(_item(format=format))

    assert (asset.width, asset.height) == (1080, 1920)
    assert asset.mime_type == "video/mp4"
    assert asset.duration_seconds is not None
    assert 1 <= asset.duration_seconds <= 8
    assert asset.path.stat().st_size < 4_000_000


def test_mixed_persian_latin_text_does_not_crash(tmp_path):
    from pv_growth.content.media import MediaRenderer

    asset = MediaRenderer(tmp_path).render(
        _item(
            title="خرید VPN — PV Network",
            body="اتصال ساده | Android / iOS / Windows — تست و خرید",
        )
    )

    assert asset.path.exists()
    assert asset.path.stat().st_size > 1_000


def test_renderer_cleanup_removes_only_expired_assets(tmp_path):
    from pv_growth.content.media import MediaRenderer

    renderer = MediaRenderer(tmp_path)
    expired = renderer.render(_item(title="old"))
    current = renderer.render(_item(title="new"))
    now = datetime.now(UTC)
    old = (now - timedelta(hours=72)).timestamp()
    os.utime(expired.path, (old, old))

    removed = renderer.cleanup(retention_hours=48, now=now)

    assert expired.path in removed and not expired.path.exists()
    assert current.path.exists()


def test_missing_public_media_store_fails_closed(settings):
    from pv_growth.content.media_store import build_media_store

    unconfigured = settings.model_copy(update={"media_public_base_url": ""})
    with pytest.raises(NotConfigured, match="public media store"):
        build_media_store(unconfigured)


def test_local_media_store_returns_public_url_without_secret(tmp_path, settings):
    from pv_growth.content.media import MediaRenderer
    from pv_growth.content.media_store import LocalMediaStore

    asset = MediaRenderer(tmp_path / "render").render(_item())
    store = LocalMediaStore(
        root=tmp_path / "public",
        public_base_url="https://media.example/pv",
        retention_hours=48,
    )
    public = store.put(asset, "campaign/post.jpg")

    assert public.url == "https://media.example/pv/campaign/post.jpg"
    assert "token" not in public.url.lower()
    assert (tmp_path / "public/campaign/post.jpg").exists()
    assert public.expires_at > datetime.now(UTC)


def test_local_media_store_cleanup_enforces_retention(tmp_path):
    from pv_growth.content.media import MediaRenderer
    from pv_growth.content.media_store import LocalMediaStore

    asset = MediaRenderer(tmp_path / "render").render(_item(title="store retention"))
    store = LocalMediaStore(
        root=tmp_path / "public",
        public_base_url="https://media.example/pv",
        retention_hours=48,
    )
    store.put(asset, "instagram/expired.jpg")
    stored = tmp_path / "public/instagram/expired.jpg"
    now = datetime.now(UTC)
    old = (now - timedelta(hours=72)).timestamp()
    os.utime(stored, (old, old))

    removed = store.cleanup(now=now)

    assert stored in removed
    assert not stored.exists()
