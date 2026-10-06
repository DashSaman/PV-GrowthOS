import importlib
import importlib.util
from datetime import UTC, date, timedelta

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, FreeAllocation, PublishedPost
from pv_growth.database.types import utcnow
from pv_growth.provisioning.xui import client_email_for
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


class Backend:
    def __init__(self):
        self.calls = []

    def health(self):
        return True

    def service_state(self, ref):
        return {
            "exists": True,
            "client_id": "uuid",
            "sub_id": "test",
            "enabled": True,
            "expired": False,
            "quota_exhausted": False,
            "traffic_limit_bytes": 1024**3,
        }

    def create_temp_service(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "client_id": "uuid",
            "sub_id": "test",
            "service_ref": client_email_for(kwargs["idempotency_key"]),
            "config_uri": "https://sub.example/link/test",
            "traffic_bytes": kwargs["traffic_bytes"],
            "expiry_ts_ms": int((utcnow() + timedelta(hours=24)).replace(tzinfo=UTC).timestamp() * 1000),
        }


def module():
    assert importlib.util.find_spec("pv_growth.free_config.shared_public"), (
        "daily original public pool missing"
    )
    return importlib.import_module("pv_growth.free_config.shared_public")


@pytest.fixture(autouse=True)
def fixed_publish_day(monkeypatch):
    monkeypatch.setattr(module(), "local_day", lambda: date(2026, 10, 6))


def setup(session, settings):
    session.add(
        AppConfig(key="pv_free_growth_policy", value={"public_shared_enabled": True, "lottery_enabled": True})
    )
    session.commit()
    s = settings.model_copy(
        update={
            "flag_free_config_enabled": True,
            "flag_public_config_enabled": True,
            "flag_pv_exclusive_config_enabled": True,
            "free_channel_id": "@pvnetwork_freeconfig",
        }
    )
    t = FakeTelegramTransport()
    return s, FlagService(s), TelegramClient(t, s), t


def test_shared_slot_is_exact_one_gib_and_not_resent(session, settings, monkeypatch):
    p = module()
    s, flags, telegram, transport = setup(session, settings)
    backend = Backend()
    monkeypatch.setattr(p, "verified_uri", lambda service, s: ("vless://uuid@8.8.8.8:443", 20))
    first = p.publish_shared_slot(session, s, flags, telegram, backend, day=date(2026, 10, 6), slot=1)
    assert first is not None
    assert p.publish_shared_slot(session, s, flags, telegram, backend, day=date(2026, 10, 6), slot=1) is None
    assert len(backend.calls) == 1
    assert backend.calls[0]["traffic_bytes"] == 1024**3
    assert backend.calls[0]["validity_hours"] == 24
    text = [v["text"] for method, v in transport.calls if method == "sendMessage"][0]
    assert "مشترک" in text and "۲۴" in text
    assert "vless://" not in text
    assert len(session.query(PublishedPost).all()) == 1


def test_failed_actual_health_never_publishes_and_keeps_budget(session, settings, monkeypatch):
    p = module()
    s, flags, telegram, transport = setup(session, settings)
    monkeypatch.setattr(
        p, "verified_uri", lambda service, s: (_ for _ in ()).throw(ValidationError("failed real probe"))
    )
    assert (
        p.publish_shared_slot(session, s, flags, telegram, Backend(), day=date(2026, 10, 6), slot=1) is None
    )
    assert not transport.calls
    assert session.query(FreeAllocation).one().traffic_bytes == 1024**3


def test_main_channel_never_is_a_destination(session, settings, monkeypatch):
    p = module()
    s, flags, telegram, transport = setup(session, settings)
    s = s.model_copy(update={"free_channel_id": "-1003855234264"})
    backend = Backend()
    assert p.publish_shared_slot(session, s, flags, telegram, backend, day=date(2026, 10, 6), slot=1) is None
    assert not transport.calls and not backend.calls


def test_eleventh_slot_is_refused(session, settings):
    p = module()
    s, flags, telegram, transport = setup(session, settings)
    backend = Backend()
    assert p.publish_shared_slot(session, s, flags, telegram, backend, day=date(2026, 10, 6), slot=11) is None
    assert not backend.calls


def test_shared_expired_config_is_replaced_by_purchase_cta_once(session, settings, monkeypatch):
    p = module()
    s, flags, telegram, transport = setup(session, settings)
    backend = Backend()
    monkeypatch.setattr(p, "verified_uri", lambda service, s: ("vless://uuid@8.8.8.8:443", 20))
    p.publish_shared_slot(session, s, flags, telegram, backend, day=date(2026, 10, 6), slot=1)
    backend.service_state = lambda ref: {"exists": True, "enabled": True, "quota_exhausted": True}
    assert p.sweep_shared_posts(session, s, telegram, backend) == 1
    assert p.sweep_shared_posts(session, s, telegram, backend) == 0
    edits = [v for method, v in transport.calls if method == "editMessageText"]
    assert len(edits) == 1
    assert "vless://" not in edits[0]["text"]
    assert "حجم" in edits[0]["text"] and "pvnetwork_bot" in edits[0]["text"]


def test_shared_panel_exhaustion_during_probe_prevents_publication(session, settings, monkeypatch):
    p = module()
    s, flags, telegram, transport = setup(session, settings)
    backend = Backend()

    def probe(*args):
        original = backend.service_state
        backend.service_state = lambda ref: {**original(ref), "quota_exhausted": True}
        return "vless://uuid@8.8.8.8:443", 20

    monkeypatch.setattr(p, "verified_uri", probe)
    assert p.publish_shared_slot(session, s, flags, telegram, backend, day=date(2026, 10, 6), slot=1) is None
    assert not transport.calls
