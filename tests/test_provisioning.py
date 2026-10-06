"""B5 provisioning tests — the 17 required scenarios against a mock X-UI
panel (same HTTP contract as the real interface discovered on production)."""

import json
import ssl
import uuid
from datetime import date, datetime

import httpx
import pytest

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import Campaign, ExclusiveClaim
from pv_growth.events.service import get_or_create_user
from pv_growth.free_config.exclusive import claim_exclusive
from pv_growth.provisioning.guard import GuardDenied
from pv_growth.provisioning.xui import XUIProvisioningAdapter, client_email_for


class FakeXUI:
    """Mock of the real panel API (Bearer + /panel/api/...)."""

    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.fail_add = False  # 500 on add
        self.timeout = False
        self.created_calls = 0
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.timeout:
            raise httpx.ConnectTimeout("simulated")
        path = request.url.path
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer tok":
            return httpx.Response(401, json={"success": False, "msg": "unauthorized"})
        if path == "/panel/api/server/status":
            return httpx.Response(200, json={"success": True})
        if path == "/panel/api/clients/add":
            if self.fail_add:
                return httpx.Response(500, text="boom")
            body = json.loads(request.content)
            client = body["client"]
            email = client["email"]
            if email in self.clients:  # panel rejects duplicates
                return httpx.Response(200, json={"success": False, "msg": "client already exists"})
            self.created_calls += 1
            self.clients[email] = {**client, "up": 0, "down": 0}
            return httpx.Response(200, json={"success": True, "msg": "created"})
        if path.startswith("/panel/api/clients/get/"):
            email = path.rsplit("/", 1)[-1]
            if email not in self.clients:
                return httpx.Response(200, json={"success": False, "msg": "not found"})
            return httpx.Response(200, json={"success": True, "obj": self.clients[email]})
        if path.startswith("/panel/api/clients/del/"):
            email = path.rsplit("/", 1)[-1]
            self.deleted.append(email)
            self.clients.pop(email, None)
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"success": False})

    # helper: what the panel REALLY holds
    def panel_client(self, email):
        return self.clients.get(email)


def make_adapter(fake: FakeXUI, settings: Settings) -> XUIProvisioningAdapter:
    return XUIProvisioningAdapter(
        settings.model_copy(
            update={
                "provisioning_base_url": "https://panel.test",
                "provisioning_token": "tok",
                "provisioning_sublink": "https://sub.test/link",
            }
        ),
        transport=httpx.MockTransport(fake.handler),
    )


@pytest.fixture
def fake_panel():
    return FakeXUI()


@pytest.fixture
def adapter(fake_panel, settings):
    return make_adapter(fake_panel, settings)


def _flags(settings):
    return FlagService(
        settings.model_copy(
            update={"flag_free_config_enabled": True, "flag_pv_exclusive_config_enabled": True}
        )
    )


@pytest.fixture
def campaign(session):
    c = Campaign(
        code=f"exc_{uuid.uuid4().hex[:6]}",
        name="ProvTest",
        kind="free_config_exclusive",
        status="active",
        start_at=datetime(2026, 1, 1),
        end_at=datetime(2027, 1, 1),
        config={
            "traffic_gb": 1,
            "validity_hours": 24,
            "location": "multiloc",
            "protocol": "vless",
            "max_claims": 3,
            "per_user_limit": 1,
        },
    )
    session.add(c)
    session.flush()
    session.commit()  # release the write lock before any nested session runs
    return c


def _user(session, tg):
    u, _ = get_or_create_user(session, telegram_user_id=tg)
    return u


# ---- adapter-level ----


def test_1_successful_provision(adapter, fake_panel):
    out = adapter.create_temp_service(
        location="multiloc", traffic_gb=1, validity_hours=24, protocol="vless", idempotency_key="k1"
    )
    email = out["service_ref"]
    assert email.startswith("growth-")
    assert out["config_uri"].startswith("https://sub.test/link/")
    row = fake_panel.panel_client(email)
    assert row is not None  # upstream service visible
    assert row["totalGB"] == 1024**3  # quota correct (1GB)
    assert row["expiryTime"] > 0  # expiry present
    assert row["enable"] is True


def test_2_duplicate_claim_creates_one_service(session, settings, fake_panel, campaign):
    adapter = make_adapter(fake_panel, settings)
    u = _user(session, 88001)
    c1, _ = claim_exclusive(
        session,
        settings,
        _flags(settings),
        adapter,
        campaign_code=campaign.code,
        user_id=u.id,
        day=date(2026, 10, 4),
    )
    c2, created2 = claim_exclusive(
        session,
        settings,
        _flags(settings),
        adapter,
        campaign_code=campaign.code,
        user_id=u.id,
        day=date(2026, 10, 4),
    )
    assert created2 is False and c2.id == c1.id
    assert fake_panel.created_calls == 1


def test_3_duplicate_worker_execution_is_safe(session, settings, fake_panel, campaign):
    """A retried job calls create_temp_service twice with the same key —
    the deterministic email makes the panel reject the second add and the
    adapter returns the existing service."""
    adapter = make_adapter(fake_panel, settings)
    a = adapter.create_temp_service(
        location="x", traffic_gb=1, validity_hours=24, protocol="vless", idempotency_key="dup-key"
    )
    b = adapter.create_temp_service(
        location="x", traffic_gb=1, validity_hours=24, protocol="vless", idempotency_key="dup-key"
    )
    assert a["service_ref"] == b["service_ref"]
    assert fake_panel.created_calls == 1
    assert b["replayed"] is True


def test_4_timeout_raises_provisioning_error(adapter, fake_panel):
    fake_panel.timeout = True
    from pv_growth.core.errors import ExternalServiceError

    with pytest.raises(ExternalServiceError):
        adapter.create_temp_service(
            location="x", traffic_gb=1, validity_hours=1, protocol="vless", idempotency_key="t"
        )


def test_5_upstream_500(session, settings, fake_panel, campaign):
    fake_panel.fail_add = True
    adapter = make_adapter(fake_panel, settings)
    u = _user(session, 88002)
    claim, _ = claim_exclusive(
        session,
        settings,
        _flags(settings),
        adapter,
        campaign_code=campaign.code,
        user_id=u.id,
        day=date(2026, 10, 4),
    )
    assert claim.status == "pending_provision"  # queued, not lost, no fake config


def test_6_partial_failure_no_fake_config(session, settings, fake_panel, campaign):
    """Panel returns success=False on add AND not-found on get."""

    class HalfBroken(FakeXUI):
        def handler(self, request):
            if request.url.path == "/panel/api/clients/add":
                return httpx.Response(200, json={"success": False, "msg": "weird"})
            return super().handler(request)

    fake = HalfBroken()
    adapter = make_adapter(fake, settings)
    u = _user(session, 88003)
    claim, _ = claim_exclusive(
        session,
        settings,
        _flags(settings),
        adapter,
        campaign_code=campaign.code,
        user_id=u.id,
        day=date(2026, 10, 4),
    )
    assert claim.status == "pending_provision"
    assert not claim.config_payload.get("config_uri")


def test_7_retry_after_failure_succeeds(session, settings, fake_panel, campaign):
    fake_panel.fail_add = True
    adapter = make_adapter(fake_panel, settings)
    u = _user(session, 88004)
    claim, _ = claim_exclusive(
        session,
        settings,
        _flags(settings),
        adapter,
        campaign_code=campaign.code,
        user_id=u.id,
        day=date(2026, 10, 4),
    )
    assert claim.status == "pending_provision"
    fake_panel.fail_add = False
    out = adapter.create_temp_service(
        location="multiloc",
        traffic_gb=1,
        validity_hours=24,
        protocol="vless",
        idempotency_key=claim.claim_key,
    )
    claim.service_ref = out["service_ref"]
    claim.config_payload = {"config_uri": out["config_uri"]}
    claim.status = "active"
    assert fake_panel.created_calls == 1


def test_8_user_already_has_claim(session, settings, fake_panel, campaign):
    adapter = make_adapter(fake_panel, settings)
    u = _user(session, 88005)
    claim_exclusive(
        session,
        settings,
        _flags(settings),
        adapter,
        campaign_code=campaign.code,
        user_id=u.id,
        day=date(2026, 10, 4),
    )
    with pytest.raises(ValidationError):
        claim_exclusive(
            session,
            settings,
            _flags(settings),
            adapter,
            campaign_code=campaign.code,
            user_id=u.id,
            day=date(2026, 10, 5),
        )


def test_9_campaign_exhausted(session, settings, fake_panel, campaign):
    adapter = make_adapter(fake_panel, settings)
    ids = []
    for tg in (88006, 88007, 88008):  # max_claims=3
        u = _user(session, tg)
        ids.append(u.id)
        claim_exclusive(
            session,
            settings,
            _flags(settings),
            adapter,
            campaign_code=campaign.code,
            user_id=u.id,
            day=date(2026, 10, 4),
        )
    u9 = _user(session, 88009)
    with pytest.raises(ValidationError):
        claim_exclusive(
            session,
            settings,
            _flags(settings),
            adapter,
            campaign_code=campaign.code,
            user_id=u9.id,
            day=date(2026, 10, 4),
        )
    assert fake_panel.created_calls == 3


def test_10_expired_campaign(session, settings, fake_panel):
    c = Campaign(
        code=f"exc_{uuid.uuid4().hex[:6]}",
        name="Old",
        kind="free_config_exclusive",
        status="active",
        start_at=datetime(2020, 1, 1),
        end_at=datetime(2020, 2, 1),
        config={"traffic_gb": 1, "validity_hours": 24, "max_claims": 5, "per_user_limit": 1},
    )
    session.add(c)
    session.flush()
    adapter = make_adapter(fake_panel, settings)
    u = _user(session, 88010)
    with pytest.raises(ValidationError):
        claim_exclusive(session, settings, _flags(settings), adapter, campaign_code=c.code, user_id=u.id)


def test_11_quota_and_12_expiry_on_panel(adapter, fake_panel):
    out = adapter.create_temp_service(
        location="x", traffic_gb=2, validity_hours=48, protocol="vless", idempotency_key="q"
    )
    row = fake_panel.panel_client(out["service_ref"])
    assert row["totalGB"] == 2 * 1024**3  # quota correct (2GB)
    assert row["expiryTime"] > 0  # expiry present
    state = adapter.service_state(out["service_ref"])
    assert state["traffic_limit_gb"] == 2
    assert state["exists"] and state["enabled"]


def test_13_config_returned_is_real_subscription_link(adapter):
    out = adapter.create_temp_service(
        location="x", traffic_gb=1, validity_hours=24, protocol="vless", idempotency_key="cfg"
    )
    assert out["config_uri"] and out["config_uri"].startswith("https://sub.test/link/")
    assert "growth-" in out["service_ref"]


def test_14_service_state_and_disable(adapter, fake_panel):
    out = adapter.create_temp_service(
        location="x", traffic_gb=1, validity_hours=1, protocol="vless", idempotency_key="st"
    )
    state = adapter.service_state(out["service_ref"])
    assert state["exists"] is True and state["enabled"] is True
    assert adapter.disable_service(out["service_ref"]) is True
    assert fake_panel.panel_client(out["service_ref"]) is None  # removed upstream


def test_15_replayed_telegram_update(settings, fake_panel, campaign):
    """A replayed Telegram callback update must not provision twice."""
    adapter = make_adapter(fake_panel, settings)
    import pv_growth.api.webhooks as wh
    import pv_growth.provisioning.xui as xui_mod
    from pv_growth.api.webhooks import process_update
    from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient

    _orig_get = xui_mod.get_provisioning
    _orig_settings = wh.get_settings
    _orig_tel = wh._telegram
    fake_tg = TelegramClient(FakeTelegramTransport(), settings)
    wh._telegram = lambda: fake_tg
    xui_mod.get_provisioning = lambda s: adapter
    wh.get_settings = lambda: settings.model_copy(
        update={
            "flag_free_config_enabled": True,
            "flag_pv_exclusive_config_enabled": True,
            "provisioning_base_url": "https://panel.test",
            "provisioning_token": "tok",
            "provisioning_sublink": "https://sub.test/link",
        }
    )
    try:
        upd = {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 88011, "username": "prov_user"},
                "data": f"claim:{campaign.code}",
            },
        }
        process_update(upd)
        process_update(upd)  # replayed
    finally:
        xui_mod.get_provisioning = _orig_get
        wh.get_settings = _orig_settings
        wh._telegram = _orig_tel
    assert fake_panel.created_calls == 1
    from pv_growth.database.base import session_scope

    with session_scope(settings) as fresh:
        claims = fresh.query(ExclusiveClaim).filter_by(campaign_id=campaign.id).all()
        assert len(claims) == 1
        assert claims[0].status == "active"


def test_16_guard_blocks_on_budget_and_health(session, settings, fake_panel):
    from pv_growth.provisioning.guard import check

    adapter = make_adapter(fake_panel, settings)
    # healthy + under budget -> passes
    check(session, settings.model_copy(update={"free_daily_budget": 50}), adapter)
    # budget exhausted -> denied
    with pytest.raises(GuardDenied):
        check(session, settings.model_copy(update={"free_daily_budget": 0}), adapter)

    # backend unhealthy -> denied (adapter-level failure)
    class DeadAdapter:
        def health(self):
            return False

    with pytest.raises(GuardDenied):
        check(session, settings, DeadAdapter())


def test_17_no_paid_service_modification(fake_panel):
    """GrowthOS only touches its own growth-* clients — the namespace prefix
    guarantees paid (Mirza) clients can never collide."""
    paid = "amir_paid_user"
    fake_panel.clients[paid] = {"email": paid, "totalGB": 10**12}
    refs = [client_email_for(f"key{i}") for i in range(100)]
    assert all(r.startswith("growth-") for r in refs)
    assert paid not in refs


def test_disable_service_refuses_non_growth_identity(adapter, fake_panel):
    fake_panel.clients["paid-user"] = {"email": "paid-user", "totalGB": 10**12}

    with pytest.raises(ValidationError, match="GrowthOS-owned"):
        adapter.disable_service("paid-user")

    assert fake_panel.deleted == []
    assert "paid-user" in fake_panel.clients


def test_get_or_none_requires_exact_returned_email(settings):
    class _WrongIdentity(FakeXUI):
        def handler(self, request):
            if request.url.path.startswith("/panel/api/clients/get/"):
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "obj": {"email": "growth-someone-else", "subId": "wrong"},
                    },
                )
            return super().handler(request)

    adapter = make_adapter(_WrongIdentity(), settings)

    assert adapter._get_or_none("growth-requested") is None


@pytest.mark.parametrize(
    ("verify_enabled", "ca_bundle", "expected"),
    [
        (True, "", True),
        (False, "", False),
        (True, "/etc/pv-growth/xui-ca.pem", "custom-ca"),
    ],
)
def test_xui_tls_verification_is_explicit(settings, monkeypatch, verify_enabled, ca_bundle, expected):
    captured = {}
    ca_context = ssl.create_default_context()

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("pv_growth.provisioning.xui.httpx.Client", _Client)
    monkeypatch.setattr(
        "pv_growth.provisioning.xui.ssl.create_default_context",
        lambda *, cafile: ca_context,
    )
    configured = settings.model_copy(
        update={
            "provisioning_base_url": "https://panel.test",
            "provisioning_token": "tok",
            "provisioning_tls_verify": verify_enabled,
            "provisioning_ca_bundle": ca_bundle,
        }
    )

    XUIProvisioningAdapter(configured)

    if expected == "custom-ca":
        assert captured["verify"] is ca_context
    else:
        assert captured["verify"] is expected
