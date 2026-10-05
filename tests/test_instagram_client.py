"""Supported Meta Instagram API boundary contract tests."""

from __future__ import annotations

import httpx
import pytest

from pv_growth.core.errors import NotConfigured


def _configured(settings, **overrides):
    values = {
        "instagram_account_id": "ig-acct-42",
        "instagram_access_token": "TEST-SECRET-TOKEN",
        "instagram_api_version": "v-test",
        "instagram_timeout_seconds": 7.5,
    }
    values.update(overrides)
    return settings.model_copy(update=values)


def _fake_client(settings):
    from pv_growth.instagram.client import FakeInstagramTransport, InstagramClient

    transport = FakeInstagramTransport()
    return InstagramClient(_configured(settings), transport=transport), transport


def test_instagram_client_refuses_missing_required_config(settings):
    from pv_growth.instagram.client import InstagramClient

    with pytest.raises(NotConfigured):
        InstagramClient(settings)


def test_create_photo_post_uses_image_url_and_caption(settings):
    client, transport = _fake_client(settings)
    transport.canned[("POST", "/ig-acct-42/media")] = {"id": "container-photo"}

    result = client.create_container(
        format="post", caption="قیمت واقعی", media_url="https://cdn.example/card.jpg"
    )

    assert result["id"] == "container-photo"
    assert transport.calls[-1] == {
        "method": "POST",
        "path": "/ig-acct-42/media",
        "params": None,
        "data": {"image_url": "https://cdn.example/card.jpg", "caption": "قیمت واقعی"},
        "headers": None,
    }


def test_create_reel_can_request_resumable_upload(settings):
    client, transport = _fake_client(settings)
    transport.canned[("POST", "/ig-acct-42/media")] = {
        "id": "container-reel",
        "uri": "https://rupload.example/session-1",
    }

    result = client.create_container(format="reel", caption="reel", resumable=True)

    assert result["uri"].endswith("session-1")
    assert transport.calls[-1]["data"] == {
        "media_type": "REELS",
        "caption": "reel",
        "upload_type": "resumable",
    }


def test_create_story_uses_story_media_type(settings):
    client, transport = _fake_client(settings)
    transport.canned[("POST", "/ig-acct-42/media")] = {"id": "container-story"}

    client.create_container(
        format="story", caption="ignored for story", media_url="https://cdn.example/story.mp4",
        is_video=True,
    )

    assert transport.calls[-1]["data"] == {
        "media_type": "STORIES",
        "video_url": "https://cdn.example/story.mp4",
    }


def test_upload_status_publish_and_insights_contract(settings):
    client, transport = _fake_client(settings)
    transport.canned[("POST", "https://rupload.example/session-1")] = {"success": True}
    transport.canned[("GET", "/container-1")] = {"status_code": "FINISHED"}
    transport.canned[("POST", "/ig-acct-42/media_publish")] = {"id": "media-1"}
    transport.canned[("GET", "/media-1/insights")] = {
        "data": [{"name": "reach", "values": [{"value": 120}]}]
    }

    assert client.upload_video("https://rupload.example/session-1", b"video") == {"success": True}
    assert client.container_status("container-1")["status_code"] == "FINISHED"
    assert client.publish_container("container-1")["id"] == "media-1"
    assert client.media_insights("media-1", ["reach", "views"])["data"][0]["name"] == "reach"
    assert transport.calls[-1]["params"] == {"metric": "reach,views"}


def test_http_transport_uses_explicit_timeout_and_redacts_token(settings):
    from pv_growth.instagram.client import HttpInstagramTransport, InstagramAPIError

    seen_timeout = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeout.update(request.extensions["timeout"])
        return httpx.Response(400, json={
            "error": {
                "type": "OAuthException",
                "code": 190,
                "message": "bad token TEST-SECRET-TOKEN",
            }
        })

    configured = _configured(settings)
    transport = HttpInstagramTransport(configured, transport=httpx.MockTransport(handler))

    with pytest.raises(InstagramAPIError) as caught:
        transport.request("GET", "/me")

    assert seen_timeout["read"] == 7.5
    assert caught.value.retryable is False
    assert caught.value.status_code == 400
    assert "TEST-SECRET-TOKEN" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_http_transport_classifies_rate_limit_and_server_errors_retryable(settings, status_code):
    from pv_growth.instagram.client import HttpInstagramTransport, InstagramAPIError

    transport = HttpInstagramTransport(
        _configured(settings),
        transport=httpx.MockTransport(lambda request: httpx.Response(
            status_code, json={"error": {"type": "UpstreamError", "code": status_code}}
        )),
    )

    with pytest.raises(InstagramAPIError) as caught:
        transport.request("GET", "/me")

    assert caught.value.retryable is True
