"""Instagram growth-loop schema, config, and kill-switch tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from pv_growth.core.flags import FLAG_KEYS, FlagService
from pv_growth.database.models import ContentItem


def _item(session, marker: str) -> ContentItem:
    item = ContentItem(
        title=f"schema-{marker}",
        kind="purchase_cta",
        channel="instagram",
        body="safe body",
        facts={"price": 1500},
        dedupe_key=f"ig-schema:{marker}:{uuid.uuid4().hex}",
    )
    session.add(item)
    session.flush()
    return item


def test_instagram_schema_models_and_content_creative_fields(session):
    from pv_growth.database.models import ContentInsight, ContentPublication

    item = _item(session, "fields")
    item.format = "reel"
    item.creative = {"template": "price-card", "hook": "یک تست واقعی"}
    publication = ContentPublication(content_id=item.id, provider="instagram", status="pending")
    session.add(publication)
    session.flush()
    insight = ContentInsight(publication_id=publication.id, metrics={"reach": 12})
    session.add(insight)
    session.flush()

    assert item.format == "reel"
    assert item.creative["template"] == "price-card"
    assert publication.attempt == 0
    assert insight.metrics == {"reach": 12}


def test_publication_remote_identity_is_unique(session):
    from pv_growth.database.models import ContentPublication

    first = _item(session, "unique-a")
    second = _item(session, "unique-b")
    session.add(ContentPublication(
        content_id=first.id, provider="instagram", status="published", media_id="ig-media-123"
    ))
    session.flush()
    session.add(ContentPublication(
        content_id=second.id, provider="instagram", status="published", media_id="ig-media-123"
    ))

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_instagram_feature_flag_defaults_off(settings):
    assert "INSTAGRAM_AUTOMATION_ENABLED" in FLAG_KEYS
    assert FlagService(settings).snapshot()["INSTAGRAM_AUTOMATION_ENABLED"] is False


def test_instagram_credentials_are_empty_until_explicitly_configured(settings):
    assert settings.instagram_account_id == ""
    assert settings.instagram_access_token == ""
    assert settings.instagram_api_version == ""
    assert settings.media_public_base_url == ""
    assert settings.media_store_token == ""


def test_db_kill_switch_overrides_environment_enable(settings, session):
    from pv_growth.database.models import FeatureFlag

    session.query(FeatureFlag).filter_by(key="INSTAGRAM_AUTOMATION_ENABLED").delete()
    session.add(FeatureFlag(key="INSTAGRAM_AUTOMATION_ENABLED", enabled=False))
    session.commit()
    configured = settings.model_copy(update={"flag_instagram_automation_enabled": True})

    assert FlagService(configured).enabled("INSTAGRAM_AUTOMATION_ENABLED") is False

    session.query(FeatureFlag).filter_by(key="INSTAGRAM_AUTOMATION_ENABLED").delete()
    session.commit()
