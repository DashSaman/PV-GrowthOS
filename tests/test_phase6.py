"""Phase 6 acceptance: analytics reconcile with events; experiments
deterministic + immutable; admin API authed; telegram webhook flows."""

import uuid

from pv_growth.analytics import queries
from pv_growth.core.errors import ValidationError
from pv_growth.database.models import Event, Experiment, User
from pv_growth.events.service import get_or_create_user, ingest
from pv_growth.experiments import service as experiments

ADMIN = {"X-Admin-Token": "test-admin-token"}


def _mk_users(session, n, prefix):
    ids = []
    for i in range(n):
        u, _ = get_or_create_user(session, telegram_user_id=int(f"96{prefix}{i}"))
        ids.append(u.id)
    return ids


def test_funnel_reconciles_with_events(session):
    users = _mk_users(session, 3, 10)
    for uid in users:
        ingest(session, "BOT_STARTED", user_id=uid, idempotency_key=f"f1:{uid}")
        ingest(session, "TRIAL_CREATED", user_id=uid, idempotency_key=f"f2:{uid}")
    ingest(session, "PRICING_VIEWED", user_id=users[0], idempotency_key=f"f3:{users[0]}")
    ingest(session, "PAYMENT_SUCCESS", user_id=users[0], idempotency_key=f"f4:{users[0]}",
           metadata={"amount_cents": 500})
    session.flush()

    f = queries.funnel(session)
    # our cohort flows through the funnel (others may exist in the shared DB)
    assert f["BOT_STARTED"] >= 3 and f["TRIAL_CREATED"] >= 3
    assert f["PRICING_VIEWED"] >= 1 and f["PAYMENT_SUCCESS"] >= 1
    # reconciliation: funnel numbers never exceed real event counts, and every
    # step this test triggered is present
    for step, value in f.items():
        raw = session.query(Event).filter_by(event_type=step).count()
        assert value <= raw
    for triggered in ("BOT_STARTED", "TRIAL_CREATED", "PRICING_VIEWED", "PAYMENT_SUCCESS"):
        assert f[triggered] >= 1
    payers = session.query(Event).filter_by(
        event_type="PAYMENT_SUCCESS", user_id=users[0]).count()
    assert payers == 1
    assert queries.revenue_summary(session)["total_cents"] >= 500


def test_experiment_deterministic_and_immutable(session):
    key = f"exp_{uuid.uuid4().hex[:6]}"
    experiments.create_experiment(session, key=key, name="Copy test",
                                  variants=["A", "B"])
    users = _mk_users(session, 5, 20)
    first = [experiments.assign(session, uid, key) for uid in users]
    second = [experiments.assign(session, uid, key) for uid in users]
    assert first == second  # never re-assigned while running
    assert all(v in {"A", "B"} for v in first)

    # deterministic: same seed+key+user always lands in the same variant
    exp = session.query(Experiment).filter_by(key=key).one()
    for uid, variant in zip(users, first, strict=True):
        assert experiments.deterministic_variant(exp, uid) == variant

    from pv_growth.database.models import ExperimentAssignment
    assignments = session.query(ExperimentAssignment).filter_by(
        experiment_id=exp.id).count()
    assert assignments == 5  # exactly one persisted row per user

    # ended experiments refuse assignment
    experiments.end_experiment(session, key)
    try:
        experiments.assign(session, users[0], key)
        raise AssertionError("should refuse")
    except ValidationError:
        pass


def test_experiment_results_report_not_declare(session):
    key = f"exp_{uuid.uuid4().hex[:6]}"
    experiments.create_experiment(session, key=key, name="Small",
                                  variants=["A", "B"])
    users = _mk_users(session, 3, 30)
    for uid in users:
        experiments.assign(session, uid, key)
    report = experiments.results(session, key)
    assert report["declared_winner"] is None  # never auto-declare
    assert report["sufficient_sample"] is False  # weak sample flagged


def test_admin_requires_token(client):
    assert client.get("/admin/api/dashboard").status_code == 401
    assert client.get("/admin/api/dashboard",
                      headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_admin_dashboard_and_flags(client, session):
    ingest(session, "BOT_STARTED", user_id=_mk_users(session, 1, 40)[0],
           idempotency_key="dash:1")
    resp = client.get("/admin/api/dashboard", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["funnel"]["BOT_STARTED"] >= 1
    assert "LIFECYCLE_AUTOMATION_ENABLED" in body["flags"]
    assert "system" in body and "queue" in body["system"] and "healthy" in body["system"]


def test_admin_flag_toggle_and_campaign_crud(client):
    flags = client.get("/admin/api/flags", headers=ADMIN).json()
    assert flags["FREE_CONFIG_ENABLED"] is False  # default off

    put = client.put("/admin/api/flags/FREE_CONFIG_ENABLED", headers=ADMIN,
                     json={"enabled": True})
    assert put.status_code == 200 and put.json()["enabled"] is True

    code = f"camp_{uuid.uuid4().hex[:6]}"
    created = client.post("/admin/api/campaigns", headers=ADMIN, json={
        "code": code, "name": "Test", "kind": "purchase", "config": {"price": 5}})
    assert created.status_code == 201
    dup = client.post("/admin/api/campaigns", headers=ADMIN, json={
        "code": code, "name": "Dup", "kind": "purchase"})
    assert dup.status_code == 409
    patched = client.patch(f"/admin/api/campaigns/{code}", headers=ADMIN,
                           json={"status": "active", "config": {"price": 9}})
    assert patched.status_code == 200 and patched.json()["status"] == "active"


def test_telegram_webhook_secret_and_flows(client, monkeypatch, settings):
    # webhook refuses wrong secret
    resp = client.post("/webhooks/telegram/wrong-secret", json={
        "update_id": 1,
        "message": {"chat": {"id": 5}, "from": {"id": 111},
                    "text": "/start freecfg_test"}})
    assert resp.status_code == 404

    # configured token + fake transport
    monkeypatch.setattr(settings, "telegram_bot_token", "T", raising=False)
    from pv_growth.telegram.client import FakeTelegramTransport
    fake = FakeTelegramTransport()
    monkeypatch.setattr("pv_growth.api.webhooks._telegram",
                        lambda: __import__("pv_growth.telegram.client",
                                           fromlist=["TelegramClient"]
                                           ).TelegramClient(fake, settings))

    tg_id = int(f"97{uuid.uuid4().int % 10000}")
    resp = client.post("/webhooks/telegram/dev", json={
        "update_id": 2,
        "message": {"chat": {"id": tg_id}, "from": {"id": tg_id, "first_name": "Reza"},
                    "text": "/start freecfg_winter"}})
    assert resp.status_code == 200

    from pv_growth.core.config import get_settings as gs
    from pv_growth.database.base import session_scope
    with session_scope(gs()) as session:
        user = session.query(User).filter_by(telegram_user_id=tg_id).one()
        assert user.telegram_chat_id == tg_id
        started = session.query(Event).filter_by(
            user_id=user.id, event_type="BOT_STARTED").count()
        assert started == 1
        # attribution survived the deep link (acceptance from Phase 2)
        from pv_growth.attribution.service import first_touch
        touch = first_touch(session, user.id)
        assert touch is not None and touch.raw_start_param == "freecfg_winter"
    texts = fake.sent_texts()
    assert any("خوش آمدید" in t for t in texts)
