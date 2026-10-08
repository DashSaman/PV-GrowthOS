from datetime import datetime

from pv_growth.database.models import AppConfig, Job, MessageLog
from pv_growth.events.service import get_or_create_user
from pv_growth.jobs.scheduler import enqueue_free_growth


def test_daily_invite_is_exact_today_slot_no_catchup_and_no_retry(session, settings):
    session.add(AppConfig(key="pv_acquisition_policy", value={"enabled": True, "daily_cap": 100}))
    session.commit()
    for now in [datetime(2026, 10, 8, 6, 14), datetime(2026, 10, 8, 7, 0)]:
        enqueue_free_growth(session, settings, now)
    assert not session.query(Job).filter_by(job_type="acquisition.daily_invite").count()
    for _ in range(2):
        enqueue_free_growth(session, settings, datetime(2026, 10, 8, 6, 15))
    row = session.query(Job).filter_by(job_type="acquisition.daily_invite").one()
    assert row.max_attempts == 1 and row.payload["day"] == "2026-10-08"


def test_every_prior_invite_attempt_is_excluded_forever(session, settings):
    from pv_growth.acquisition.daily import previously_invited

    user, _ = get_or_create_user(session, telegram_user_id=144, telegram_chat_id=144)
    session.add(
        MessageLog(
            user_id=user.id,
            template_code="old",
            purpose="never_buyer_invite_20261008",
            dedupe_key="old",
            status="failed",
            meta={"error_code": 403},
        )
    )
    session.flush()
    assert previously_invited(session, user.id)


def test_private_receipt_requires_exact_text_buttons_and_recipient():
    from pv_growth.acquisition.daily import verified_receipt

    receipt = {
        "chat": {"id": 144, "type": "private"},
        "message_id": 1,
        "date": 1,
        "text": "body",
        "reply_markup": {"inline_keyboard": []},
    }
    assert verified_receipt(receipt, 144, "body", receipt["reply_markup"])
    assert not verified_receipt(receipt, 145, "body", receipt["reply_markup"])
    assert not verified_receipt(receipt, 144, "changed", receipt["reply_markup"])
    assert not verified_receipt(receipt, 144, "body", {"inline_keyboard": [[]]})


def test_daily_policy_rejects_unbounded_and_noninteger_caps(session):
    from pv_growth.acquisition.daily import enabled_policy

    for cap in [1000, True, "100", 0]:
        row = session.get(AppConfig, "pv_acquisition_policy")
        if row is None:
            row = AppConfig(key="pv_acquisition_policy", value={})
            session.add(row)
        row.value = {"enabled": True, "daily_cap": cap}
        session.flush()
        assert enabled_policy(session) is None


def prepare_run(session, settings, monkeypatch, contacts=(144, 145), cap=1):
    from pv_growth.acquisition import daily

    session.add(AppConfig(key="pv_acquisition_policy", value={"enabled": True, "daily_cap": cap}))
    session.commit()
    monkeypatch.setattr(daily, "two_owned_posts", lambda *args: True)
    s = settings.model_copy(update={"purchase_bot_token": "123:test"})

    class Reader:
        def list_private_invite_contacts(self, **kwargs):
            assert kwargs["limit"] == 5000
            return list(contacts)

        def fetch_private_invite_history(self, recipient):
            return [], [], []

    class Bot:
        sent = 0

        def call(self, method, payload):
            if method == "getMe":
                return {"id": 123, "username": "pvnetwork_bot", "is_bot": True}
            if method == "getChat":
                return {"id": payload["chat_id"], "type": "private"}
            from pv_growth.database.base import session_scope

            with session_scope(s) as other:
                assert other.query(MessageLog).filter_by(status="reserved").count() == 1
            self.sent += 1
            return {
                "chat": {"id": payload["chat_id"], "type": "private"},
                "message_id": self.sent,
                "date": 1,
                "text": payload["text"],
                "reply_markup": payload["reply_markup"],
            }

    return daily, s, Reader(), Bot()


def test_daily_send_is_durable_capped_and_cannot_be_replayed(session, settings, monkeypatch):
    daily, s, reader, bot = prepare_run(session, settings, monkeypatch)
    day = daily.local_day().isoformat()
    result = daily.run_daily(session, s, day, reader=reader, bot=bot, adapter=object())
    assert result["sent"] == 1 and result["attempted"] == 1 and bot.sent == 1
    assert (
        daily.run_daily(session, s, day, reader=reader, bot=bot, adapter=object())["status"]
        == "already_attempted"
    )
    assert bot.sent == 1


def test_purchase_after_reservation_blocks_send(session, settings, monkeypatch):
    daily, s, reader, bot = prepare_run(session, settings, monkeypatch, contacts=(144,))
    calls = 0

    def history(recipient):
        nonlocal calls
        calls += 1
        return ([], [], []) if calls == 1 else ([{"Status": "active"}], [], [])

    reader.fetch_private_invite_history = history
    result = daily.run_daily(
        session, s, daily.local_day().isoformat(), reader=reader, bot=bot, adapter=object()
    )
    assert result["sent"] == 0 and bot.sent == 0
    assert session.query(MessageLog).one().status == "audience_blocked"


def test_missing_receipt_is_terminal_and_previous_failed_invites_excluded(session, settings, monkeypatch):
    daily, s, reader, bot = prepare_run(session, settings, monkeypatch, contacts=(144, 145), cap=2)
    user, _ = get_or_create_user(session, telegram_user_id=144, telegram_chat_id=144)
    session.add(
        MessageLog(
            user_id=user.id,
            template_code="old",
            purpose="never_buyer_invite_20261008",
            dedupe_key="old",
            status="failed",
            meta={"error_code": 403},
        )
    )
    session.commit()
    original = bot.call

    def call(method, payload):
        if method == "sendMessage":
            return {"message_id": 1}
        return original(method, payload)

    bot.call = call
    result = daily.run_daily(
        session, s, daily.local_day().isoformat(), reader=reader, bot=bot, adapter=object()
    )
    assert result["attempted"] == 1 and result["sent"] == 0 and result["status"] == "api_stopped"
    assert session.query(MessageLog).filter_by(status="delivery_unknown").count() == 1
