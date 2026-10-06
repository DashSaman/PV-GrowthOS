"""Small synchronous client for supported Instagram content APIs.

The Graph base URL and API version are configuration, not code assumptions.
Tests use ``FakeInstagramTransport`` so no Meta credential or network is needed.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from pv_growth.core.config import Settings
from pv_growth.core.errors import ExternalServiceError, NotConfigured, ValidationError


class InstagramAPIError(ExternalServiceError):
    def __init__(self, *, status_code: int | None, retryable: bool, category: str, detail: str) -> None:
        self.status_code = status_code
        self.retryable = retryable
        self.category = category
        self.detail = detail[:240]
        status = str(status_code) if status_code is not None else "transport"
        super().__init__(f"instagram api {status} ({category}): {self.detail}")


class InstagramTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | bytes | None = None,
        headers: dict | None = None,
    ) -> dict: ...


class HttpInstagramTransport:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.instagram_access_token or not settings.instagram_api_version:
            raise NotConfigured("Instagram access token/API version not configured")
        self._token = settings.instagram_access_token
        base = settings.instagram_api_base.rstrip("/")
        version = settings.instagram_api_version.strip("/")
        self._client = httpx.Client(
            base_url=f"{base}/{version}/",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            timeout=settings.instagram_timeout_seconds,
            transport=transport,
        )

    def _safe(self, value: str) -> str:
        return value.replace(self._token, "[REDACTED]")[:240]

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | bytes | None = None,
        headers: dict | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if isinstance(data, bytes):
            kwargs["content"] = data
        else:
            kwargs["data"] = data
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise InstagramAPIError(
                status_code=None,
                retryable=True,
                category="transport",
                detail=type(exc).__name__,
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise InstagramAPIError(
                status_code=response.status_code,
                retryable=response.status_code >= 500,
                category="invalid_response",
                detail="non-json response",
            ) from exc

        if response.status_code >= 400:
            remote = body.get("error") if isinstance(body, dict) else {}
            remote = remote if isinstance(remote, dict) else {}
            category = str(remote.get("type") or remote.get("code") or "http_error")
            detail = self._safe(str(remote.get("message") or category))
            raise InstagramAPIError(
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
                category=category[:64],
                detail=detail,
            )
        if not isinstance(body, dict):
            raise InstagramAPIError(
                status_code=response.status_code,
                retryable=False,
                category="invalid_response",
                detail="JSON object required",
            )
        return body


class FakeInstagramTransport:
    """Specific, network-free transport for contract and service tests."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.canned: dict[tuple[str, str], dict] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | bytes | None = None,
        headers: dict | None = None,
    ) -> dict:
        normalized = method.upper()
        self.calls.append(
            {
                "method": normalized,
                "path": path,
                "params": params,
                "data": data,
                "headers": headers,
            }
        )
        return dict(self.canned.get((normalized, path), {"id": f"fake-{len(self.calls)}"}))


class InstagramClient:
    def __init__(self, settings: Settings, transport: InstagramTransport | None = None) -> None:
        if (
            not settings.instagram_account_id
            or not settings.instagram_access_token
            or not settings.instagram_api_version
        ):
            raise NotConfigured(
                "Instagram requires professional account ID, access token, and explicit API version"
            )
        self._settings = settings
        self._transport = transport or HttpInstagramTransport(settings)

    def create_container(
        self,
        *,
        format: str,
        caption: str,
        media_url: str | None = None,
        is_video: bool = False,
        resumable: bool = False,
    ) -> dict:
        if format == "post":
            if not media_url:
                raise NotConfigured("Instagram photo post requires a public media URL")
            data = {"image_url": media_url, "caption": caption}
        elif format == "reel":
            data = {"media_type": "REELS", "caption": caption}
            if resumable:
                data["upload_type"] = "resumable"
            elif media_url:
                data["video_url"] = media_url
            else:
                raise NotConfigured("Instagram reel requires media URL or resumable upload")
        elif format == "story":
            data = {"media_type": "STORIES"}
            if resumable and is_video:
                data["upload_type"] = "resumable"
            elif media_url:
                data["video_url" if is_video else "image_url"] = media_url
            else:
                raise NotConfigured("Instagram story requires media URL or resumable video upload")
        else:
            raise ValidationError(f"unsupported Instagram format: {format}")
        return self._transport.request("POST", f"/{self._settings.instagram_account_id}/media", data=data)

    def upload_video(self, upload_uri: str, content: bytes) -> dict:
        return self._transport.request(
            "POST",
            upload_uri,
            data=content,
            headers={
                "Authorization": f"OAuth {self._settings.instagram_access_token}",
                "offset": "0",
                "file_size": str(len(content)),
            },
        )

    def container_status(self, container_id: str) -> dict:
        return self._transport.request("GET", f"/{container_id}", params={"fields": "status_code,status"})

    def publish_container(self, container_id: str) -> dict:
        return self._transport.request(
            "POST",
            f"/{self._settings.instagram_account_id}/media_publish",
            data={"creation_id": container_id},
        )

    def owned_media(self, *, limit: int = 25) -> dict:
        if limit < 1 or limit > 100:
            raise ValidationError("Instagram owned-media limit must be between 1 and 100")
        return self._transport.request(
            "GET",
            f"/{self._settings.instagram_account_id}/media",
            params={
                "fields": "id,caption,media_type,permalink,timestamp",
                "limit": limit,
            },
        )

    def media_insights(self, media_id: str, metrics: list[str]) -> dict:
        if not metrics:
            raise ValidationError("at least one Instagram insight metric is required")
        return self._transport.request("GET", f"/{media_id}/insights", params={"metric": ",".join(metrics)})
