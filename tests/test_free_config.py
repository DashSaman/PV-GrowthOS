"""Phase 2 acceptance: pipeline dedup, invalid-not-published, idempotent
publishing (two scheduler runs cannot duplicate), exclusive quota/overrides."""

import base64
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier

import httpx
import pytest

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import (
    Campaign,
    ConfigSource,
    Event,
    ExclusiveClaim,
    Job,
    PublishedPost,
    RawConfig,
)
from pv_growth.events.service import get_or_create_user
from pv_growth.free_config.exclusive import (
    FakeProvisioningClient,
    claim_exclusive,
    resolve_day_plan,
)
from pv_growth.free_config.pipeline import (
    collect_and_stage,
    publish_public_slot,
    render_public_post,
)
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def _vmess(host="1.2.3.4", port=443, uid="uid-1", remark="R"):
    payload = {"ps": remark, "add": host, "port": str(port), "id": uid, "net": "ws", "tls": "tls"}
    return "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()


class FakeFetchTransport(httpx.MockTransport):
    """Serves two sources: one good, one with garbage + duplicates."""

    def __init__(self) -> None:
        super().__init__(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "good" in str(request.url):
            body = "\n".join(
                [
                    _vmess(uid="uid-a", remark="A"),
                    _vmess(uid="uid-a", remark="A-dup"),  # duplicate (same cred)
                    _vmess(uid="uid-b", remark="B"),
                ]
            )
            return httpx.Response(200, text=body)
        if "bad" in str(request.url):
            return httpx.Response(200, text="vmess://broken!!!\nnot a config at all")
        return httpx.Response(404)


@pytest.fixture
def env(settings, session):
    http = httpx.Client(transport=FakeFetchTransport())
    session.add_all(
        [
            ConfigSource(kind="github_file", name=f"good{uuid.uuid4().hex[:6]}", url="https://x/good.txt"),
            ConfigSource(kind="github_file", name=f"bad{uuid.uuid4().hex[:6]}", url="https://x/bad.txt"),
        ]
    )
    session.flush()
    flags = FlagService(settings)
    telegram = TelegramClient(FakeTelegramTransport(), settings)
    return {"http": http, "flags": flags, "telegram": telegram, "settings": settings}


def test_collect_dedups_and_rejects_invalid(session, env):
    stats = collect_and_stage(session, env["http"], env["settings"])
    assert stats["sources"] == 2
    assert stats["new"] == 2  # uid-a counted once (dup dropped), uid-b once
    assert stats["duplicates"] == 1
    assert stats["invalid"] >= 1  # broken base64 + plain text


def _flags_on(settings: Settings) -> FlagService:
    return FlagService(
        settings.model_copy(
            update={
                "flag_free_config_enabled": True,
                "flag_public_config_enabled": True,
                "flag_pv_exclusive_config_enabled": True,
            }
        )
    )


def test_publish_is_idempotent_double_run(session, env, monkeypatch):
    collect_and_stage(session, env["http"], env["settings"])
    monkeypatch.setattr(
        "pv_growth.free_config.pipeline.check_top_candidates",
        lambda candidates, s: {cid: True for cid, _, _ in candidates},
    )
    settings: Settings = env["settings"]
    flags = _flags_on(settings)
    monkeypatch.setattr(settings, "free_channel_id", "@freech", raising=False)

    day = date(2026, 10, 3)
    post1 = publish_public_slot(session, settings, flags, env["telegram"], day=day, slot=1)
    assert post1 is not None

    # second scheduler run, same day+slot: MUST NOT create a second post
    post2 = publish_public_slot(session, settings, flags, env["telegram"], day=day, slot=1)
    assert post2 is None
    assert session.query(PublishedPost).filter_by(kind="free_public").count() == 1

    sent = [p.get("text", "") for m, p in env["telegram"]._t.calls if m == "sendMessage"]
    assert len(sent) == 1  # acceptance: two scheduled posts cannot duplicate
    assert "کامانیوتی" in sent[0] or "PV Network" in sent[0]  # public label present
    # The channel promise is an actually usable free config, not just a label.
    published = session.query(PublishedPost).filter_by(kind="free_public").one()
    config = session.get(RawConfig, published.config_id)
    assert config.raw_uri in sent[0]
    payload = [p for m, p in env["telegram"]._t.calls if m == "sendMessage"][0]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/pvnetwork_bot"


def test_public_post_escapes_untrusted_remark():
    config = RawConfig(
        source_id=1,
        uri_hash="safe-render",
        protocol="vless",
        host="example.test",
        port=443,
        raw_uri="vless://id@example.test:443?x=1&y=2",
        remark="<b>not markup</b>",
    )

    rendered = render_public_post(config)

    assert "&lt;b&gt;not markup&lt;/b&gt;" in rendered
    assert "x=1&amp;y=2" in rendered


def test_unhealthy_configs_never_publish(session, env, monkeypatch):
    unique_day = date(2026, 11, 3)
    collect_and_stage(session, env["http"], env["settings"])
    monkeypatch.setattr(
        "pv_growth.free_config.pipeline.check_top_candidates",
        lambda candidates, s: {cid: False for cid, _, _ in candidates},  # all unhealthy
    )
    settings: Settings = env["settings"]
    flags = _flags_on(settings)
    monkeypatch.setattr(settings, "free_channel_id", "@freech", raising=False)
    assert publish_public_slot(session, settings, flags, env["telegram"], day=unique_day, slot=1) is None
    assert session.query(PublishedPost).filter_by(dedupe_key=f"free_public:{unique_day}:1").count() == 0


def test_publish_disabled_by_default_flags(session, env):
    settings: Settings = env["settings"]  # flags default OFF
    assert (
        publish_public_slot(session, settings, env["flags"], env["telegram"], day=date(2026, 10, 3), slot=1)
        is None
    )


# ---------- exclusive pool ----------


@pytest.fixture
def exclusive_campaign(session):
    campaign = Campaign(
        code=f"exc_{uuid.uuid4().hex[:8]}",
        name="Exc",
        kind="free_config_exclusive",
        status="active",
        start_at=datetime(2026, 1, 1),
        end_at=datetime(2027, 1, 1),
        config={
            "traffic_gb": 5,
            "validity_hours": 24,
            "location": "nl",
            "protocol": "vless",
            "max_claims": 2,
            "per_user_limit": 1,
            "date_overrides": {"2026-10-04": {"traffic_gb": 10}},
        },
    )
    session.add(campaign)
    session.flush()
    return campaign


def test_exclusive_day_plan_overrides(exclusive_campaign):
    assert resolve_day_plan(exclusive_campaign, date(2026, 10, 3)).traffic_gb == 5
    assert resolve_day_plan(exclusive_campaign, date(2026, 10, 4)).traffic_gb == 10
    assert resolve_day_plan(exclusive_campaign, date(2026, 10, 4)).location == "nl"


def test_exclusive_claim_flow(session, settings, exclusive_campaign):
    flags, provisioning = _flags_on(settings), FakeProvisioningClient()
    user, _ = get_or_create_user(session, telegram_user_id=7101)

    claim, created = claim_exclusive(
        session,
        settings,
        flags,
        provisioning,
        campaign_code=exclusive_campaign.code,
        user_id=user.id,
        day=date(2026, 10, 3),
    )
    assert created is True and claim.status == "active"
    assert claim.traffic_gb == 5 and claim.service_ref
    assert provisioning.created[0]["location"] == "nl"

    # idempotent re-claim same day
    claim2, created2 = claim_exclusive(
        session,
        settings,
        flags,
        provisioning,
        campaign_code=exclusive_campaign.code,
        user_id=user.id,
        day=date(2026, 10, 3),
    )
    assert created2 is False and claim2.id == claim.id

    # per_user_limit=1 -> next day rejected
    with pytest.raises(ValidationError):
        claim_exclusive(
            session,
            settings,
            flags,
            provisioning,
            campaign_code=exclusive_campaign.code,
            user_id=user.id,
            day=date(2026, 10, 4),
        )


def test_exclusive_quota_exhausted(session, settings, exclusive_campaign):
    flags, provisioning = _flags_on(settings), FakeProvisioningClient()
    ids = []
    for tg in (7201, 7202, 7203):
        u, _ = get_or_create_user(session, telegram_user_id=tg)
        ids.append(u.id)

    claim_exclusive(
        session,
        settings,
        flags,
        provisioning,
        campaign_code=exclusive_campaign.code,
        user_id=ids[0],
        day=date(2026, 10, 3),
    )
    claim_exclusive(
        session,
        settings,
        flags,
        provisioning,
        campaign_code=exclusive_campaign.code,
        user_id=ids[1],
        day=date(2026, 10, 3),
    )
    with pytest.raises(ValidationError):  # max_claims=2 exhausted
        claim_exclusive(
            session,
            settings,
            flags,
            provisioning,
            campaign_code=exclusive_campaign.code,
            user_id=ids[2],
            day=date(2026, 10, 3),
        )
    assert session.query(ExclusiveClaim).filter_by(campaign_id=exclusive_campaign.id).count() == 2


def test_exclusive_daily_budget_one_allows_exactly_one_claim(session, settings, exclusive_campaign):
    flags = _flags_on(settings)
    provisioning = FakeProvisioningClient()
    bounded = settings.model_copy(update={"free_daily_budget": 1})
    claim_day = date.today()
    first, _ = get_or_create_user(session, telegram_user_id=7251)
    second, _ = get_or_create_user(session, telegram_user_id=7252)

    claim, created = claim_exclusive(
        session,
        bounded,
        flags,
        provisioning,
        campaign_code=exclusive_campaign.code,
        user_id=first.id,
        day=claim_day,
    )

    assert created is True and claim.status == "active"
    with pytest.raises(ValidationError, match="daily free budget"):
        claim_exclusive(
            session,
            bounded,
            flags,
            provisioning,
            campaign_code=exclusive_campaign.code,
            user_id=second.id,
            day=claim_day,
        )
    assert session.query(ExclusiveClaim).filter_by(claim_date=claim_day).count() == 1


def test_exclusive_unknown_campaign(session, settings):
    flags, provisioning = _flags_on(settings), FakeProvisioningClient()
    user, _ = get_or_create_user(session, telegram_user_id=7301)
    with pytest.raises(ValidationError):
        claim_exclusive(
            session, settings, flags, provisioning, campaign_code="does_not_exist", user_id=user.id
        )


def test_provisioning_failure_leaves_claim_queued(session, settings, exclusive_campaign):
    flags = _flags_on(settings)

    class BrokenProvisioning:
        def health(self):
            return True  # guard passes; the create call itself fails

        def create_temp_service(self, **kwargs):
            raise RuntimeError("endpoint down")

    user, _ = get_or_create_user(session, telegram_user_id=7401)
    claim, created = claim_exclusive(
        session,
        settings,
        flags,
        BrokenProvisioning(),
        campaign_code=exclusive_campaign.code,
        user_id=user.id,
        day=date(2026, 10, 3),
    )
    assert created is True
    assert claim.status == "pending_provision"  # queued, not lost
    assert session.query(Job).filter_by(idempotency_key=f"provision:{claim.claim_key}").count() == 1
    assert session.query(Event).filter_by(idempotency_key=f"trial:{claim.claim_key}").count() == 0


def test_provisioning_retry_activates_once_and_emits_trial_once(
    session, settings, exclusive_campaign, monkeypatch
):
    session.query(Job).filter_by(job_type="provisioning.claim").delete()
    session.commit()
    flags = _flags_on(settings)

    class BrokenProvisioning:
        def health(self):
            return True

        def create_temp_service(self, **kwargs):
            raise RuntimeError("endpoint down")

    user, _ = get_or_create_user(session, telegram_user_id=7402)
    claim, _ = claim_exclusive(
        session,
        settings,
        flags,
        BrokenProvisioning(),
        campaign_code=exclusive_campaign.code,
        user_id=user.id,
        day=date(2026, 10, 8),
    )
    session.commit()
    recovered = FakeProvisioningClient()
    monkeypatch.setattr(
        "pv_growth.provisioning.lifecycle_job.get_provisioning",
        lambda _settings: recovered,
    )
    from pv_growth.jobs.runner import run_tick

    assert run_tick(settings, worker_id="provision-retry", batch=1) == 1
    session.expire_all()
    claim = session.get(ExclusiveClaim, claim.id)
    assert claim.status == "active"
    assert claim.provision_attempts == 2
    assert len(recovered.created) == 1
    assert session.query(Event).filter_by(idempotency_key=f"trial:{claim.claim_key}").count() == 1


def test_provisioning_retry_exhaustion_is_terminal(session, settings, exclusive_campaign, monkeypatch):
    session.query(Job).filter_by(job_type="provisioning.claim").delete()
    session.commit()
    flags = _flags_on(settings)

    class BrokenProvisioning:
        def health(self):
            return True

        def create_temp_service(self, **kwargs):
            raise RuntimeError("still down")

    bounded = settings.model_copy(update={"job_max_attempts_default": 1})
    user, _ = get_or_create_user(session, telegram_user_id=7403)
    claim, _ = claim_exclusive(
        session,
        bounded,
        flags,
        BrokenProvisioning(),
        campaign_code=exclusive_campaign.code,
        user_id=user.id,
        day=date(2026, 10, 9),
    )
    session.commit()
    monkeypatch.setattr(
        "pv_growth.provisioning.lifecycle_job.get_provisioning",
        lambda _settings: BrokenProvisioning(),
    )
    from pv_growth.jobs.runner import run_tick

    assert run_tick(bounded, worker_id="provision-terminal", batch=1) == 1
    session.expire_all()
    claim = session.get(ExclusiveClaim, claim.id)
    assert claim.status == "provision_failed"
    assert claim.provision_attempts == 2
    assert claim.last_provision_error
    assert session.query(Event).filter_by(idempotency_key=f"trial:{claim.claim_key}").count() == 0


def test_postgres_concurrent_claims_cannot_oversubscribe_daily_budget(session, settings, exclusive_campaign):
    if os.environ.get("TEST_DB") != "postgres":
        pytest.skip("row-lock concurrency proof runs in the PostgreSQL CI matrix")

    from pv_growth.database.base import session_scope

    bounded = settings.model_copy(update={"free_daily_budget": 1})
    claim_day = date(2026, 12, 31)
    users = [
        get_or_create_user(session, telegram_user_id=7491)[0],
        get_or_create_user(session, telegram_user_id=7492)[0],
    ]
    user_ids = [user.id for user in users]
    campaign_code = exclusive_campaign.code
    session.commit()
    barrier = Barrier(2)

    def attempt(user_id: int) -> str:
        with session_scope(bounded) as worker_session:
            barrier.wait(timeout=5)
            try:
                claim_exclusive(
                    worker_session,
                    bounded,
                    _flags_on(bounded),
                    FakeProvisioningClient(),
                    campaign_code=campaign_code,
                    user_id=user_id,
                    day=claim_day,
                )
            except ValidationError:
                return "denied"
            return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, user_ids))

    assert outcomes == ["created", "denied"]
    with session_scope(bounded) as verify:
        assert verify.query(ExclusiveClaim).filter_by(claim_date=claim_day).count() == 1
