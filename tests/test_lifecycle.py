"""Phase 3 acceptance: lifecycle guard rails — purchase stops reminders,
cooldown, max sends, idempotent sends; segments recalculable; strict templates."""

import uuid

import pytest

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.core.flags import FlagService
from pv_growth.database.models import LifecycleRule, MessageLog, MessageTemplate
from pv_growth.events.service import get_or_create_user, ingest
from pv_growth.lifecycle.service import scan, send_followup
from pv_growth.messaging import service as messaging
from pv_growth.segments.service import compute_segment, recompute_all
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


def _flags_on(settings: Settings) -> FlagService:
    return FlagService(settings.model_copy(update={"flag_lifecycle_automation_enabled": True}))


@pytest.fixture
def msg_env(settings, session):
    code = uuid.uuid4().hex[:8]
    session.add(MessageTemplate(code=f"tpl_{code}", intent="trial_followup",
                                body="سلام {{first_name}}! تست {{traffic_gb}} گیگ شما آماده است."))
    session.add(LifecycleRule(
        code=f"rule_{code}", trigger="trial_no_purchase", template_code=f"tpl_{code}",
        cooldown_hours=24, max_sends=1, stop_conditions=["PAYMENT_SUCCESS"],
    ))
    session.flush()
    tg = TelegramClient(FakeTelegramTransport(), settings)
    return {"code": code, "tg": tg, "tpl": f"tpl_{code}", "rule": f"rule_{code}"}


def _user_with_trial(session, tg_id, chat=True):
    user, _ = get_or_create_user(session, telegram_user_id=tg_id,
                                 first_name="Test", telegram_chat_id=tg_id if chat else None)
    ingest(session, "BOT_STARTED", user_id=user.id,
           idempotency_key=f"bs:{tg_id}")
    ingest(session, "TRIAL_CREATED", user_id=user.id,
           idempotency_key=f"tc:{tg_id}")
    ingest(session, "TRIAL_CONNECTED", user_id=user.id,
           idempotency_key=f"tconn:{tg_id}")
    session.flush()
    return user


def test_purchase_stops_sales_reminder(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8101)
    # user converts BEFORE the followup fires
    ingest(session, "PAYMENT_SUCCESS", user_id=user.id,
           idempotency_key=f"pay:{user.id}",
           metadata={"order_id": "o1", "amount_cents": 100})
    session.flush()

    status = send_followup(session, settings, flags, msg_env["tg"],
                           {"rule_code": msg_env["rule"], "user_id": user.id})
    assert status == "stopped"  # acceptance: purchasers stop receiving wrong reminders
    assert session.query(MessageLog).count() == 0
    assert msg_env["tg"]._t.calls == []


def test_send_respects_cooldown_and_max_sends(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8102)

    payload = {"rule_code": msg_env["rule"], "user_id": user.id, "facts": {"traffic_gb": 5}}
    first = send_followup(session, settings, flags, msg_env["tg"], payload)
    assert first == "sent"

    # immediate second run: blocked by max_sends=1 (and cooldown 24h)
    second = send_followup(session, settings, flags, msg_env["tg"], payload)
    assert second in {"cooldown", "max_sends", "duplicate"}
    sent = [t for m, t in []]  # noqa: F841
    texts = [p.get("text", "") for m, p in msg_env["tg"]._t.calls if m == "sendMessage"]
    assert len(texts) == 1  # exactly one message ever


def test_send_is_idempotent_on_dedupe_key(settings, session, msg_env):
    user = _user_with_trial(session, 8103)
    row1, created1 = messaging.send_user_message(
        session, msg_env["tg"], user_id=user.id, template_code=msg_env["tpl"],
        purpose="manual", facts={"traffic_gb": 5}, dedupe_key=f"m:{user.id}:1")
    row2, created2 = messaging.send_user_message(
        session, msg_env["tg"], user_id=user.id, template_code=msg_env["tpl"],
        purpose="manual", facts={"traffic_gb": 5}, dedupe_key=f"m:{user.id}:1")
    assert created1 is True and created2 is False
    assert row1.id == row2.id


def test_scan_schedules_jobs_idempotently(settings, session, msg_env):
    flags = _flags_on(settings)
    user = _user_with_trial(session, 8104)
    scheduled1 = scan(session, settings, flags)
    scan(session, settings, flags)  # second scan: idempotent enqueue
    assert scheduled1 >= 1
    # second scan finds the same candidates but enqueue is idempotent
    from pv_growth.database.models import Job
    count = session.query(Job).filter(
        Job.idempotency_key == f"lc:{msg_env['rule']}:{user.id}").count()
    assert count == 1


def test_scan_disabled_by_flag(settings, session, msg_env):
    flags = FlagService(settings)  # default OFF
    _user_with_trial(session, 8105)
    assert scan(session, settings, flags) == 0


def test_template_render_strict():
    body = "قیمت {{price}} تومان، حجم {{traffic_gb}} گیگ، اعتبار {{validity_hours}} ساعت"
    ok = messaging.render(body, {"price": 99000, "traffic_gb": 10, "validity_hours": 30})
    assert "99000" in ok
    with pytest.raises(ValidationError):  # missing fact -> refuse, never invent
        messaging.render(body, {"price": 99000})
    # malformed placeholder refused
    with pytest.raises(ValidationError):
        messaging.render("این {price} خراب است {{price}", {"price": 1})


def test_segments_recomputable(session):
    user, _ = get_or_create_user(session, telegram_user_id=8201)
    ingest(session, "BOT_STARTED", user_id=user.id, idempotency_key="s1:bs")
    session.flush()
    assert compute_segment(session, user) == "NEW_LEAD"

    ingest(session, "TRIAL_CREATED", user_id=user.id, idempotency_key="s1:tc")
    ingest(session, "TRIAL_CONNECTED", user_id=user.id, idempotency_key="s1:tconn")
    ingest(session, "PRICING_VIEWED", user_id=user.id, idempotency_key="s1:pv")
    session.flush()
    assert compute_segment(session, user) == "HOT_LEAD"

    ingest(session, "PAYMENT_SUCCESS", user_id=user.id, idempotency_key="s1:pay")
    session.flush()
    assert compute_segment(session, user) == "FIRST_TIME_BUYER"

    ingest(session, "PAYMENT_SUCCESS", user_id=user.id,
           idempotency_key="s1:pay2", metadata={"renewal": True})
    ingest(session, "SERVICE_RENEWED", user_id=user.id, idempotency_key="s1:ren")
    session.flush()
    assert compute_segment(session, user) == "RETURNING_CUSTOMER"

    counts = recompute_all(session)
    assert counts.get("RETURNING_CUSTOMER", 0) >= 1
