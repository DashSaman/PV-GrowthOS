"""Admin Content Engine workflow acceptance tests."""

from __future__ import annotations

import uuid

from pv_growth.database.models import ContentItem

ADMIN = {"X-Admin-Token": "test-admin-token"}


def _create_content(client, **overrides):
    payload = {
        "title": "Instagram-ready draft",
        "kind": "purchase_cta",
        "channel": "official",
        "body": "قیمت {{price}} تومان",
        "facts": {"price": 1500},
        "dedupe_key": f"admin-content:{uuid.uuid4().hex}",
    }
    payload.update(overrides)
    return client.post("/admin/api/content", headers=ADMIN, json=payload)


def test_content_admin_requires_token(client):
    assert client.get("/admin/api/content").status_code == 401


def test_content_admin_create_and_list(client):
    created = _create_content(client)

    assert created.status_code == 201
    item_id = created.json()["id"]
    assert created.json()["status"] == "draft"

    listed = client.get("/admin/api/content", headers=ADMIN)
    assert listed.status_code == 200
    match = next(row for row in listed.json() if row["id"] == item_id)
    assert match["title"] == "Instagram-ready draft"
    assert match["facts"] == {"price": 1500}


def test_content_admin_validation_rejects_contradictory_campaign_fact(client):
    campaign = f"price_{uuid.uuid4().hex[:8]}"
    created_campaign = client.post("/admin/api/campaigns", headers=ADMIN, json={
        "code": campaign,
        "name": "Fact source",
        "kind": "purchase",
        "status": "active",
        "config": {"price": 1500},
    })
    assert created_campaign.status_code == 201
    created = _create_content(client, campaign_code=campaign, facts={"price": 999})
    item_id = created.json()["id"]

    invalid = client.post(f"/admin/api/content/{item_id}/validate", headers=ADMIN)

    assert invalid.status_code == 422
    assert "contradicts campaign" in invalid.json()["detail"]
    listed = client.get("/admin/api/content", headers=ADMIN).json()
    assert next(row for row in listed if row["id"] == item_id)["status"] == "draft"


def test_content_admin_validate_then_schedule_requires_iso_datetime(client):
    created = _create_content(client)
    item_id = created.json()["id"]

    validated = client.post(f"/admin/api/content/{item_id}/validate", headers=ADMIN)
    malformed = client.post(
        f"/admin/api/content/{item_id}/schedule", headers=ADMIN, json={"when": "tomorrow-ish"}
    )
    scheduled = client.post(
        f"/admin/api/content/{item_id}/schedule",
        headers=ADMIN,
        json={"when": "2026-10-06T05:30:00+00:00"},
    )

    assert validated.status_code == 200 and validated.json()["status"] == "validated"
    assert malformed.status_code == 422
    assert scheduled.status_code == 200 and scheduled.json()["status"] == "scheduled"
    assert scheduled.json()["scheduled_at"].startswith("2026-10-06T05:30:00")


def test_content_admin_retry_allows_only_failed_item(client, session):
    created = _create_content(client)
    item_id = created.json()["id"]

    rejected = client.post(f"/admin/api/content/{item_id}/retry", headers=ADMIN)
    assert rejected.status_code == 422

    item = session.get(ContentItem, item_id)
    item.status = "failed"
    session.commit()

    retried = client.post(f"/admin/api/content/{item_id}/retry", headers=ADMIN)
    assert retried.status_code == 200
    assert retried.json()["status"] == "draft"


def test_content_admin_missing_item_is_404(client):
    response = client.post("/admin/api/content/99999999/validate", headers=ADMIN)
    assert response.status_code == 404
    assert response.json()["detail"] == "content item not found"
