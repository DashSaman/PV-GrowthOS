"""Phase 1 acceptance: event ingestion is idempotent; users resolve; API works."""

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import Event, Job
from pv_growth.events.service import get_or_create_user, ingest


def test_ingest_and_duplicate_is_idempotent(session):
    user, _ = get_or_create_user(session, telegram_user_id=1001)
    event, created = ingest(
        session,
        "BOT_STARTED",
        user_id=user.id,
        idempotency_key="botstart:1001:1",
        metadata={"chat_id": 55},
    )
    assert created is True

    event2, created2 = ingest(
        session,
        "BOT_STARTED",
        user_id=user.id,
        idempotency_key="botstart:1001:1",
        metadata={"chat_id": 55},
    )
    assert created2 is False
    assert event2.id == event.id
    count = session.query(Event).filter_by(event_type="BOT_STARTED").count()
    assert count == 1  # acceptance: duplicate events are idempotent


def test_unknown_event_type_rejected(session):
    user, _ = get_or_create_user(session, telegram_user_id=1002)
    try:
        ingest(session, "NOT_A_REAL_EVENT", user_id=user.id, idempotency_key="x1")
        raise AssertionError("should have raised")
    except ValidationError:
        pass


def test_get_or_create_user_idempotent(session):
    user1, created1 = get_or_create_user(session, telegram_user_id=2001, username="ali")
    user2, created2 = get_or_create_user(session, telegram_user_id=2001, username="ali2")
    assert created1 is True and created2 is False
    assert user1.id == user2.id
    assert user2.username == "ali2"  # profile refreshed


def test_event_api_roundtrip(client):
    headers = {"X-Admin-Token": "test-admin-token"}
    resp = client.post(
        "/api/events",
        headers=headers,
        json={
            "event_type": "PRICING_VIEWED",
            "telegram_user_id": 3001,
            "idempotency_key": "api:pv:3001:1",
            "metadata": {"plan": "monthly"},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["created"] is True

    dup = client.post(
        "/api/events",
        headers=headers,
        json={
            "event_type": "PRICING_VIEWED",
            "telegram_user_id": 3001,
            "idempotency_key": "api:pv:3001:1",
        },
    )
    assert dup.status_code == 201
    assert dup.json()["created"] is False  # duplicate reported as not-created

    listed = client.get("/api/events", headers=headers, params={"event_type": "PRICING_VIEWED"})
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_event_api_requires_token(client):
    assert client.post("/api/events", json={"event_type": "BOT_STARTED"}).status_code == 401
    assert (
        client.post(
            "/api/events",
            json={"event_type": "BOT_STARTED"},
            headers={"X-Admin-Token": "wrong"},
        ).status_code
        == 401
    )


def test_event_api_unknown_type_422(client):
    resp = client.post(
        "/api/events",
        headers={"X-Admin-Token": "test-admin-token"},
        json={
            "event_type": "BOGUS_TYPE_X",
            "idempotency_key": "b1",
        },
    )
    assert resp.status_code == 422


def test_event_api_settled_payment_enqueues_conversion(client, session):
    headers = {"X-Admin-Token": "test-admin-token"}
    response = client.post(
        "/api/events",
        headers=headers,
        json={
            "event_type": "PAYMENT_SUCCESS",
            "telegram_user_id": 3002,
            "idempotency_key": "api:pay:3002:1",
            "metadata": {"order_ref": "api-order-3002", "amount_cents": 125000},
        },
    )

    assert response.status_code == 201
    assert response.json()["created"] is True
    session.expire_all()
    job = session.query(Job).filter_by(idempotency_key="conversion:api:pay:3002:1").one()
    assert job.payload["order_ref"] == "api-order-3002"
    assert job.payload["amount_cents"] == 125000
