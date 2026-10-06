"""Phase 1 acceptance: source persists, first-touch persists and is immutable."""

from pv_growth.attribution.service import attribute, first_touch, parse_start_param
from pv_growth.database.models import SOURCE_KINDS, Campaign, ContentItem, Source
from pv_growth.events.service import get_or_create_user


def test_parse_start_param_patterns():
    assert "socialc" in SOURCE_KINDS
    assert parse_start_param("freecfg_summer").kind == "freecfg"
    assert parse_start_param("freecfg_summer").reference == "summer"
    assert parse_start_param("ref_12345").kind == "ref"
    assert parse_start_param("partner_acme").kind == "partner"
    assert parse_start_param("seo_pricing").kind == "seo"
    assert parse_start_param("channel_tg").kind == "channel"
    assert parse_start_param("socialc_42").kind == "socialc"
    assert parse_start_param("socialc_42").reference == "42"
    assert parse_start_param(None).kind == "direct"
    # hostile input degrades to direct, never crashes
    assert parse_start_param("x" * 500).kind == "direct"
    assert parse_start_param("bad\ncode").kind == "direct"


def test_first_touch_persists_and_never_overwritten(session):
    user, _ = get_or_create_user(session, telegram_user_id=5001)

    source_a, first_created = attribute(session, user.id, "freecfg_summer")
    assert first_created is True
    touch = first_touch(session, user.id)
    assert touch is not None
    assert touch.source_id == source_a.id

    # second, different source: first-touch MUST stay, last-touch updates
    source_b, first_created2 = attribute(session, user.id, "social_twitter")
    assert first_created2 is False
    touch_after = first_touch(session, user.id)
    assert touch_after.source_id == source_a.id  # immutable

    from pv_growth.database.models import AttributionTouch

    last = session.query(AttributionTouch).filter_by(user_id=user.id, kind="last").one()
    assert last.source_id == source_b.id  # last-touch follows the newest


def test_source_persists_with_counters(session):
    user, _ = get_or_create_user(session, telegram_user_id=5002)
    source, _ = attribute(session, user.id, "freecfg_counter_t")
    attribute(session, user.id, "freecfg_counter_t")  # repeat click
    session.flush()
    persisted = session.query(Source).filter_by(code="freecfg:counter_t").one()
    assert persisted.bot_starts == 2
    assert persisted.kind == "freecfg"
    assert persisted.medium == "freecfg"


def test_source_links_campaign(session):
    campaign = Campaign(code="link_t", name="LinkT", kind="free_config_exclusive", status="active", config={})
    session.add(campaign)
    session.flush()
    user, _ = get_or_create_user(session, telegram_user_id=5003)
    source, _ = attribute(session, user.id, "freecfg_link_t")
    assert source.campaign_id == campaign.id


def test_content_source_validates_item_and_links_owning_campaign(session):
    campaign = Campaign(
        code="content_link",
        name="Content Link",
        kind="purchase",
        status="active",
        config={"price": 100},
    )
    session.add(campaign)
    session.flush()
    item = ContentItem(
        title="A",
        body="body",
        channel="instagram",
        status="scheduled",
        campaign_code=campaign.code,
        dedupe_key="attr-content-a",
    )
    session.add(item)
    session.flush()
    user, _ = get_or_create_user(session, telegram_user_id=5004)

    source, _ = attribute(session, user.id, f"socialc_{item.id}")

    assert source.code == f"socialc:{item.id}"
    assert source.kind == "socialc"
    assert source.campaign_id == campaign.id


def test_unknown_content_source_degrades_to_direct_without_fabricating_source(session):
    user, _ = get_or_create_user(session, telegram_user_id=5005)

    source, _ = attribute(session, user.id, "socialc_99999999")

    assert source.code == "direct"
    assert session.query(Source).filter_by(code="socialc:99999999").count() == 0
