import importlib.util
from datetime import datetime, timedelta

import httpx
import pytest

from pv_growth.database.models import AppConfig, ContentItem, FreeAllocation, Job, PublishedPost


def research():
    assert importlib.util.find_spec("pv_growth.growth_research"), "native growth research missing"
    from pv_growth import growth_research

    return growth_research


def directory_reply(request):
    return httpx.Response(
        200,
        json={
            "results": [
                {"id": 123, "name": "فروشگاه نمونه", "domain": "shop.example", "city": "تهران"},
                {"id": 124, "name": "blocked", "domain": "bad.example", "block_partner": True},
                {"id": 125, "name": "unsafe", "domain": "127.0.0.1"},
            ]
        },
    )


def test_native_research_dedupes_day_and_has_no_customer_effects(session, settings, monkeypatch):
    r = research()
    monkeypatch.setattr(r, "utcnow", lambda: datetime(2026, 10, 16, 9))
    requests = []

    def reply(request):
        requests.append(str(request.url))
        assert request.url.host == "api.torob.com"
        return directory_reply(request)

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
    report = session.get(AppConfig, "growth-research:daily:2026-10-16").value
    assert len(requests) == 4
    assert len(report["businesses"]) == 1
    assert report["businesses"][0]["followers"] is None
    assert report["businesses"][0]["source_url"] == "https://torob.com/shop/123/"
    assert report["explore_status"] == "official_source_not_connected"
    assert report["trend_status"] == "directory_baseline_not_instagram_popularity"
    for model in (ContentItem, FreeAllocation, PublishedPost):
        assert session.query(model).count() == 0


@pytest.mark.parametrize("mode", ["oversize", "redirect", "malformed", "forbidden"])
def test_source_failure_is_saved_and_not_retried_or_reported_as_zero(session, settings, monkeypatch, mode):
    r = research()
    monkeypatch.setattr(r, "utcnow", lambda: datetime(2026, 10, 16, 9))
    requests = []

    def reply(request):
        requests.append(request)
        return {
            "oversize": lambda: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)),
            "redirect": lambda: httpx.Response(302, headers={"Location": "http://127.0.0.1/"}),
            "malformed": lambda: httpx.Response(200, json={"changed_contract": True}),
            "forbidden": lambda: httpx.Response(403),
        }[mode]()

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
    report = session.get(AppConfig, "growth-research:daily:2026-10-16").value
    assert len(requests) == 1
    assert report["status"] == "unavailable"
    assert report["failures"] and not report["complete"]
    assert "ناموجود/ناقص" in r.report_text(session, "2026-10-16")


def test_schedule_once_per_tehran_day_and_pause_controls(session):
    r = research()
    r.enqueue_daily(session, datetime(2026, 10, 16, 4, 24))
    assert session.query(Job).count() == 0
    for _ in range(2):
        r.enqueue_daily(session, datetime(2026, 10, 16, 4, 25))
    assert session.query(Job).filter_by(job_type="growth.research_daily").count() == 1
    session.add(AppConfig(key="growth_research_control", value={"enabled": False}))
    r.enqueue_daily(session, datetime(2026, 10, 17, 10))
    assert session.query(Job).count() == 1


def test_wrong_day_disabled_or_release_pause_performs_no_fetch(session, settings, monkeypatch):
    r = research()
    monkeypatch.setattr(r, "utcnow", lambda: datetime(2026, 10, 16, 9))
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected fetch"))) as client:
        r.collect_daily(session, settings, {"day": "2026-10-15"}, client)
        session.add(AppConfig(key="pv_free_growth_policy", value={"operational_pause": True}))
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
    assert session.get(AppConfig, "growth-research:daily:2026-10-16") is None


def test_report_does_not_invent_trends_and_keeps_actual_observation_date(session, settings, monkeypatch):
    r = research()
    monkeypatch.setattr(r, "utcnow", lambda: datetime(2026, 10, 16, 9))
    with httpx.Client(transport=httpx.MockTransport(directory_reply)) as client:
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
    report = session.get(AppConfig, "growth-research:daily:2026-10-16").value
    assert report["observed_at_utc"].startswith("2026-10-16T09:")
    assert all(not row["below_20k_verified"] for row in report["businesses"])
    text = r.report_text(session, "2026-10-16")
    assert "تضمین" in text and "اکسپلور" in text and "فروش نیست" in text
    assert "ناموجود" in r.report_text(session, "2026-10-17")
    draft = r.outreach_text("<b>نمونه</b>\nmalicious")
    assert "<b>" not in draft and "۵۰ گیگی" in draft and "VPN" in draft


def test_only_owner_controls_research_without_outreach(session, settings):
    from pv_growth.telegram import management as m

    class Client:
        owner = True
        sent = []

        def get_chat_member(self, channel, user):
            return {"status": "creator" if self.owner else "administrator", "user": {"id": user}}

        def send_message(self, chat, body, keyboard=None):
            self.sent.append(body)

    c = Client()
    m.handle_action(session, settings, c, user_id=42, chat_id=42, chat_type="private", action="research:off")
    assert session.get(AppConfig, "growth_research_control").value["enabled"] is False
    c.owner = False
    m.handle_action(session, settings, c, user_id=42, chat_id=42, chat_type="private", action="research:on")
    assert session.get(AppConfig, "growth_research_control").value["enabled"] is False
    assert session.query(Job).count() == session.query(FreeAllocation).count() == 0


def test_old_reports_retained_only_thirty_days(session, settings, monkeypatch):
    r = research()
    now = datetime(2026, 10, 16, 9)
    monkeypatch.setattr(r, "utcnow", lambda: now)
    for i in range(1, 40):
        session.add(AppConfig(key=f"growth-research:daily:{(now - timedelta(days=i)).date()}", value={}))
    with httpx.Client(transport=httpx.MockTransport(directory_reply)) as client:
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
    assert session.query(AppConfig).filter(AppConfig.key.startswith("growth-research:daily:")).count() == 30


def test_daily_digest_summary_is_bounded_and_registry_connected(session):
    r = research()
    from pv_growth.jobs import runner

    runner.register_builtin_handlers()
    assert "growth.research_daily" in runner._HANDLERS
    session.add(
        AppConfig(
            key="growth-research:daily:2026-10-16",
            value={
                "status": "partial",
                "complete": False,
                "observed_businesses": 12,
                "businesses": [{"brand": "نمونه", "source_url": "https://torob.com/shop/123/"}],
            },
        )
    )
    session.flush()
    text = r.nightly_summary(session, "2026-10-16")
    assert len(text) <= 550 and "ناقص" in text and "۱۲" not in text
    assert "12" in text and "فروش نیست" in text


def test_expired_partner_counts_never_qualify_below_twenty_thousand():
    r = research()
    assert not any(p["below_20k_verified"] for p in r.seed_partners("2026-10-17"))


def test_partial_response_preserves_successes_and_reservation_blocks_repeat(session, settings, monkeypatch):
    r = research()
    monkeypatch.setattr(r, "utcnow", lambda: datetime(2026, 10, 16, 9))
    calls = []

    def reply(request):
        calls.append(request)
        return directory_reply(request) if len(calls) == 1 else httpx.Response(429)

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
        session.expire_all()
        r.collect_daily(session, settings, {"day": "2026-10-16"}, client)
    report = session.get(AppConfig, "growth-research:daily:2026-10-16").value
    assert len(calls) == 2 and report["status"] == "partial" and len(report["businesses"]) == 1
