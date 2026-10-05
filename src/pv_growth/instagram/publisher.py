"""Idempotent Instagram publication with explicit ambiguity handling."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pv_growth.content.media import MediaRenderer
from pv_growth.content.media_store import MediaStore
from pv_growth.content.publishers import PublishResult
from pv_growth.core.errors import ExternalServiceError, ValidationError
from pv_growth.database.models import ContentItem, ContentPublication
from pv_growth.database.types import utcnow
from pv_growth.instagram.client import InstagramAPIError, InstagramClient


class InstagramPublisher:
    """Publish one content item without blindly repeating remote mutations."""

    provider = "instagram"
    max_attempts = 5

    def __init__(self, session: Session, client: InstagramClient, *,
                 renderer: MediaRenderer | None = None,
                 media_store: MediaStore | None = None) -> None:
        self._session = session
        self._client = client
        self._renderer = renderer
        self._media_store = media_store

    def _publication(self, item: ContentItem) -> ContentPublication:
        publication = self._session.execute(
            select(ContentPublication).where(
                ContentPublication.provider == self.provider,
                ContentPublication.content_id == item.id,
            )
        ).scalar_one_or_none()
        if publication is None:
            publication = ContentPublication(
                content_id=item.id,
                provider=self.provider,
                status="pending",
            )
            self._session.add(publication)
            self._session.flush()
        return publication

    def _record_api_error(self, publication: ContentPublication,
                          exc: InstagramAPIError, *, ambiguous: bool = False) -> bool:
        publication.error_category = exc.category
        publication.error_detail = exc.detail
        if exc.retryable:
            publication.attempt += 1
            if publication.attempt >= self.max_attempts:
                publication.status = "failed"
            else:
                publication.status = "publish_unknown" if ambiguous else "retryable_error"
        else:
            publication.status = "failed"
        self._session.flush()
        return exc.retryable and publication.attempt < self.max_attempts

    def _raise_api_error(self, publication: ContentPublication,
                         exc: InstagramAPIError, *, ambiguous: bool = False) -> None:
        can_retry = self._record_api_error(publication, exc, ambiguous=ambiguous)
        if exc.retryable and not can_retry:
            raise ExternalServiceError("Instagram retry budget exhausted") from exc
        raise exc

    def reconcile(self, publication_id: int) -> ContentPublication:
        publication = self._session.get(ContentPublication, publication_id)
        if publication is None or publication.provider != self.provider:
            raise ValidationError("Instagram publication not found")
        if publication.media_id or not publication.container_id:
            return publication

        status = self._client.container_status(publication.container_id)
        media_id = status.get("published_media_id")
        if media_id:
            publication.media_id = str(media_id)
            publication.status = "published"
            publication.error_category = None
            publication.error_detail = None
            publication.published_at = utcnow()
        elif publication.status != "publish_unknown":
            remote_status = str(status.get("status_code") or status.get("status") or "").upper()
            publication.status = "ready" if remote_status == "FINISHED" else "processing"
        self._session.flush()
        return publication

    def publish(self, item: ContentItem, rendered_body: str) -> PublishResult:
        if item.format not in {"post", "reel", "story"}:
            raise ValidationError(f"unsupported Instagram format: {item.format}")

        publication = self._publication(item)
        if publication.media_id:
            return PublishResult(
                remote_id=publication.media_id,
                metadata={"provider": self.provider, "container_id": publication.container_id},
            )

        if publication.status == "publish_unknown":
            publication = self.reconcile(publication.id)
            if publication.media_id:
                return PublishResult(
                    remote_id=publication.media_id,
                    metadata={"provider": self.provider,
                              "container_id": publication.container_id,
                              "reconciled": True},
                )
            raise ExternalServiceError(
                "Instagram publication is ambiguous; refusing a blind publish retry"
            )

        creative = dict(item.creative or {})
        if not creative.get("media_url"):
            if self._renderer is not None and self._media_store is not None:
                asset = self._renderer.render(item)
                suffix = ".mp4" if asset.mime_type == "video/mp4" else ".jpg"
                public = self._media_store.put(asset, f"instagram/{item.id}{suffix}")
                creative["media_url"] = public.url
                creative["media_expires_at"] = public.expires_at.isoformat()
                creative["is_video"] = asset.mime_type == "video/mp4"
                item.creative = creative
                self._session.flush()
        if publication.container_id is None:
            try:
                created = self._client.create_container(
                    format=item.format,
                    caption=rendered_body,
                    media_url=creative.get("media_url"),
                    is_video=bool(creative.get("is_video", item.format == "reel")),
                    resumable=False,
                )
            except InstagramAPIError as exc:
                self._raise_api_error(publication, exc)
            container_id = created.get("id")
            if not container_id:
                publication.status = "failed"
                publication.error_category = "invalid_response"
                publication.error_detail = "create container response missing id"
                self._session.flush()
                raise ExternalServiceError("Instagram create container response missing id")
            # This flush must happen before any later remote mutation.
            publication.container_id = str(container_id)
            publication.status = "container_created"
            publication.error_category = None
            publication.error_detail = None
            self._session.flush()

        try:
            status = self._client.container_status(publication.container_id)
        except InstagramAPIError as exc:
            self._raise_api_error(publication, exc)
        remote_status = str(status.get("status_code") or status.get("status") or "").upper()
        if remote_status != "FINISHED":
            publication.attempt += 1
            if publication.attempt >= self.max_attempts:
                publication.status = "failed"
                self._session.flush()
                raise ExternalServiceError("Instagram container retry budget exhausted")
            publication.status = "processing"
            self._session.flush()
            raise InstagramAPIError(
                status_code=None,
                retryable=True,
                category="container_processing",
                detail=f"status={remote_status or 'unknown'}",
            )

        publication.status = "ready"
        self._session.flush()
        try:
            published = self._client.publish_container(publication.container_id)
        except InstagramAPIError as exc:
            self._raise_api_error(publication, exc, ambiguous=exc.retryable)

        media_id = published.get("id")
        if not media_id:
            publication.status = "publish_unknown"
            publication.error_category = "invalid_response"
            publication.error_detail = "publish response missing media id"
            self._session.flush()
            raise ExternalServiceError("Instagram publish response missing media id")

        publication.media_id = str(media_id)
        publication.status = "published"
        publication.error_category = None
        publication.error_detail = None
        publication.published_at = utcnow()
        self._session.flush()
        return PublishResult(
            remote_id=publication.media_id,
            metadata={"provider": self.provider, "container_id": publication.container_id},
        )
