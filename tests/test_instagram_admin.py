"""Authenticated Instagram observability and dry-run controls."""

from __future__ import annotations

import uuid

from pv_growth.database.models import (
    Campaign,
    ContentInsight,
    ContentItem,
    ContentPublication,
)
from pv_growth.database.types import utcnow

ADMIN = {"X-Admin-Token": "test-admin-token"}


def _published(session):
    campaign = Campaign(
        code=f"admin_ig_{uuid.uuid4().hex[:8]}",
        name="IG admin",
        kind="purchase",
        status="active",
        config={"price": 1234},
    )
    session.add(campaign)
    session.flush()
    item = ContentItem(
        title="admin item",
        kind="purchase_cta",
        channel="instagram",
        format="post",
        body="caption",
        facts={"price": 1234},
        creative={"template": "brand_card"},
        status="published",
        campaign_code=campaign.code,
        published_at=utcnow(),
        dedupe_key=f"admin-ig:{uuid.uuid4().hex}",
    )
    session.add(item)
    session.flush()
    publication = ContentPublication(
        content_id=item.id,
        provider="instagram",
        status="published",
        container_id=f"container-{uuid.uuid4().hex[:8]}",
        media_id=f"media-{uuid.uuid4().hex[:8]}",
        published_at=utcnow(),
    )
    session.add(publication)
    session.flush()
    insight = ContentInsight(
        publication_id=publication.id,
        captured_at=utcnow(),
        metrics={"reach": 99, "likes": 7},
    )
    session.add(insight)
    session.commit()
    return item, publication, insight, campaign


def test_instagram_admin_endpoints_require_auth(client):
    for path in (
        "/admin/api/instagram/readiness",
        "/admin/api/instagram/publications",
        "/admin/api/instagram/insights",
        "/admin/api/instagram/rankings",
    ):
        assert client.get(path).status_code == 401
    assert client.post("/admin/api/instagram/plan/preview").status_code == 401


def test_readiness_reports_presence_but_never_secret_values(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "instagram_access_token", "TOP-SECRET-META-TOKEN")
    monkeypatch.setattr(settings, "media_store_token", "TOP-SECRET-STORE-TOKEN")
    monkeypatch.setattr(settings, "instagram_account_id", "account-123")
    monkeypatch.setattr(settings, "instagram_api_version", "v-test")

    response = client.get("/admin/api/instagram/readiness", headers=ADMIN)

    assert response.status_code == 200
    raw = response.text
    assert "TOP-SECRET-META-TOKEN" not in raw
    assert "TOP-SECRET-STORE-TOKEN" not in raw
    body = response.json()
    assert body["api_credentials_configured"] is True
    assert isinstance(body["missing"], list)


def test_publications_and_insights_are_observable_without_error_secrets(client, session):
    item, publication, insight, _ = _published(session)

    publications = client.get("/admin/api/instagram/publications", headers=ADMIN).json()
    insights = client.get("/admin/api/instagram/insights", headers=ADMIN).json()

    pub = next(row for row in publications if row["id"] == publication.id)
    snapshot = next(row for row in insights if row["id"] == insight.id)
    assert pub["content_id"] == item.id
    assert pub["media_id"] == publication.media_id
    assert "error_detail" not in pub
    assert snapshot["metrics"] == {"reach": 99, "likes": 7}


def test_plan_preview_has_no_database_side_effects(client, session):
    _, _, _, campaign = _published(session)
    content_before = session.query(ContentItem).count()
    publications_before = session.query(ContentPublication).count()

    response = client.post("/admin/api/instagram/plan/preview", headers=ADMIN)

    assert response.status_code == 200
    preview = response.json()
    assert any(row["campaign_code"] == campaign.code for row in preview)
    session.expire_all()
    assert session.query(ContentItem).count() == content_before
    assert session.query(ContentPublication).count() == publications_before


def test_rankings_expose_conversion_score_metadata(client, session):
    item, _, _, _ = _published(session)

    response = client.get("/admin/api/instagram/rankings", headers=ADMIN)

    assert response.status_code == 200
    row = next(value for value in response.json() if value["content_id"] == item.id)
    assert set(("score", "confidence", "sufficient_sample", "is_winner")) <= row.keys()


def test_admin_content_contract_includes_instagram_format_and_creative(client):
    payload = {
        "title": "manual IG draft",
        "kind": "education",
        "channel": "instagram",
        "format": "story",
        "body": "safe",
        "facts": {},
        "creative": {"template": "brand_card"},
        "dedupe_key": f"admin-format:{uuid.uuid4().hex}",
    }

    response = client.post("/admin/api/content", headers=ADMIN, json=payload)

    assert response.status_code == 201
    assert response.json()["format"] == "story"
    assert response.json()["creative"] == {"template": "brand_card"}
