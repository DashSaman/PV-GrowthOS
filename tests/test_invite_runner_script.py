"""Offline-only checks. Never invokes Docker, Telegram, Mirza, or production."""

import copy
import hashlib
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_never_buyer_invites as r  # noqa: E402

NOW = datetime(2026, 10, 6, 17, tzinfo=UTC)
BINDING = {"panel_id": 33, "active": True, "inbound_ids": [17, 21, 37, 82, 83, 125, 126, 127, 128]}


def proof(slot):
    key = f"pv_shared:2026-10-06:{slot}"
    service_ref = "growth-" + hashlib.sha256(key.encode()).hexdigest()[:16]
    return {
        "kind": "pv_shared",
        "channel_id": "-1004310246787",
        "message_id": slot,
        "posted_at": NOW - timedelta(minutes=10),
        "key": key,
        "status": "published",
        "traffic_bytes": 1024**3,
        "service_ref": service_ref,
        "expires_at": NOW + timedelta(hours=1),
        "payload": {
            "tunnel_plan": BINDING,
            "health_method": "https_via_proxy",
            "checked_at_utc": NOW.isoformat(),
            "verified_uri": "vless://test-only",
            "service": {
                "service_ref": service_ref,
                "traffic_bytes": 1024**3,
                "client_id": "offline",
                "sub_id": "offline",
                "enabled": True,
                "expiry_ts_ms": int((NOW + timedelta(hours=1)).timestamp() * 1000),
            },
        },
    }


def gift_policy():
    return {
        "gift_enabled": True,
        "gift_day": "2026-10-06",
        "gift_daily_bytes": 1000 * 1024**3,
        "gift_traffic_bytes": 100 * 1024**2,
        "gift_validity_hours": 24,
    }


def gift_campaign():
    return {
        "code": "pv_welcome_100",
        "kind": "free_config_exclusive",
        "status": "active",
        "config": {"gift": True},
        "start_at": NOW - timedelta(hours=1),
        "end_at": datetime(2026, 10, 6, 20, 30, tzinfo=UTC),
    }


class SafetyChecks(unittest.TestCase):
    def test_shared_requires_two_distinct_owned_healthy_recent_posts(self):
        self.assertTrue(r.shared_ready([proof(1), proof(2)], BINDING, NOW))
        self.assertFalse(r.shared_ready([proof(1), proof(1)], BINDING, NOW))
        for path, value in [
            ("kind", "free_public"),
            ("channel_id", "-1003855234264"),
            ("status", "delivery_unknown"),
            ("traffic_bytes", 100),
            ("expires_at", NOW),
            ("posted_at", NOW - timedelta(hours=25)),
        ]:
            rows = [proof(1), proof(2)]
            rows[1][path] = value
            self.assertFalse(r.shared_ready(rows, BINDING, NOW), path)
        for key, value in [
            ("health_method", "tcp"),
            ("verified_uri", ""),
            ("tunnel_plan", {"panel_id": 33}),
            ("checked_at_utc", (NOW - timedelta(hours=25)).isoformat()),
        ]:
            rows = [proof(1), proof(2)]
            rows[1]["payload"][key] = value
            self.assertFalse(r.shared_ready(rows, BINDING, NOW), key)

    def test_gift_exact_today_policy_and_active_campaign_only(self):
        self.assertTrue(r.gift_ready(gift_policy(), gift_campaign(), NOW))
        for key, value in [
            ("gift_enabled", False),
            ("gift_day", "2026-10-07"),
            ("gift_daily_bytes", 1024**3),
            ("gift_traffic_bytes", 99 * 1024**2),
            ("gift_validity_hours", 48),
        ]:
            p = gift_policy()
            p[key] = value
            self.assertFalse(r.gift_ready(p, gift_campaign(), NOW), key)
        self.assertFalse(r.gift_ready(gift_policy(), gift_campaign(), NOW + timedelta(days=1)))
        campaign = gift_campaign()
        campaign["status"] = "paused"
        self.assertFalse(r.gift_ready(gift_policy(), campaign, NOW))

    def test_never_buyer_complete_history_excludes_dormant_and_unknown(self):
        from pv_growth.free_config.audience import evaluate_history

        self.assertTrue(r.never_buyer(evaluate_history([], [], [], now=NOW, dormant_days=45)))
        for history in [
            ([{"Status": "active", "price_product": 0}], [], []),
            ([{"Status": "disabledn", "price_product": 100, "time_sell": "1700000000"}], [], []),
            ([], [], [{"payment_Status": "paid", "price": 100, "time": "2026/01/01 12:00:00"}]),
            ([], [{"type": "unknown", "status": "paid", "price": 0}], []),
        ]:
            self.assertFalse(r.never_buyer(evaluate_history(*history, now=NOW, dormant_days=45)))

    def test_private_destinations_require_positive_exact_existing_main_bot_chat(self):
        for value in [-1003855234264, 0, True, "123", 2**52]:
            self.assertFalse(r.private_receipt({"id": value, "type": "private"}, value))
        self.assertTrue(r.private_receipt({"id": 123, "type": "private"}, 123))
        self.assertFalse(r.private_receipt({"id": 124, "type": "private"}, 123))
        self.assertFalse(r.private_receipt({"id": 123, "type": "group"}, 123))

    def test_reservation_precedes_send_and_unknown_never_retries(self):
        calls = []

        def reserve():
            calls.append("durable_reserve")
            return True

        def fresh():
            calls.append("fresh_complete_history")
            return True

        def send():
            calls.append("send")
            return {"ok": False, "delivery_unknown": True}

        def final(status, response):
            calls.append(status)

        gate = r.RateGate()
        out = r.deliver_once(reserve=reserve, fresh=fresh, send=send, finish=final, gate=gate)
        self.assertEqual(out, "delivery_unknown")
        self.assertEqual(
            calls,
            [
                "durable_reserve",
                "fresh_complete_history",
                "fresh_complete_history",
                "send",
                "delivery_unknown",
            ],
        )
        calls.clear()
        self.assertEqual(
            r.deliver_once(reserve=lambda: False, fresh=fresh, send=send, finish=final, gate=gate),
            "dedupe_skip",
        )
        self.assertEqual(calls, [])

    def test_fresh_purchase_or_stop_after_reservation_blocks_message(self):
        calls = []
        gate = r.RateGate()
        out = r.deliver_once(
            reserve=lambda: True,
            fresh=lambda: False,
            send=lambda: calls.append("send"),
            finish=lambda status, response: calls.append(status),
            gate=gate,
        )
        self.assertEqual(out, "audience_blocked")
        self.assertNotIn("send", calls)
        gate.stop("429")
        self.assertEqual(
            r.deliver_once(
                reserve=lambda: True,
                fresh=lambda: True,
                send=lambda: calls.append("send"),
                finish=lambda *args: None,
                gate=gate,
            ),
            "paused",
        )
        self.assertNotIn("send", calls)

    def test_429_stops_entire_remaining_cohort_without_retry(self):
        gate = r.RateGate()
        count = []

        def send():
            count.append(1)
            return {"ok": False, "error_code": 429, "retry_after": 30}

        self.assertEqual(
            r.deliver_once(
                reserve=lambda: True, fresh=lambda: True, send=send, finish=lambda *args: None, gate=gate
            ),
            "failed",
        )
        self.assertTrue(gate.stopped.is_set())
        self.assertEqual(
            r.deliver_once(
                reserve=lambda: True, fresh=lambda: True, send=send, finish=lambda *args: None, gate=gate
            ),
            "paused",
        )
        self.assertEqual(len(count), 1)

    def test_global_rate_gate_spaces_dispatches_across_workers(self):
        clock = [0.0]
        sleep = []

        def wait(seconds):
            sleep.append(seconds)
            clock[0] += seconds

        gate = r.RateGate(clock=lambda: clock[0], sleep=wait)
        self.assertTrue(gate.acquire())
        self.assertTrue(gate.acquire())
        self.assertTrue(gate.acquire())
        self.assertEqual(sleep, [0.5, 0.5])

    def test_previous_canary_recent_and_uncertain_logs_are_excluded(self):
        self.assertTrue(r.log_blocks({"purpose": "bot45_20261006", "status": "failed", "meta": {}}, NOW))
        self.assertTrue(r.log_blocks({"purpose": "other", "status": "reserved", "meta": {}}, NOW))
        self.assertTrue(
            r.log_blocks(
                {"purpose": "other", "status": "sent", "sent_at": NOW - timedelta(days=2), "meta": {}}, NOW
            )
        )
        self.assertFalse(
            r.log_blocks(
                {"purpose": "other", "status": "sent", "sent_at": NOW - timedelta(days=8), "meta": {}}, NOW
            )
        )
        self.assertTrue(
            r.log_blocks(
                {"purpose": "other", "status": "failed", "meta": {"reserved_at_utc": NOW.isoformat()}}, NOW
            )
        )

    def test_copy_gift_omitted_when_disabled_and_no_fixed_ip_claim(self):
        plain, buttons = r.invitation(False)
        gift, gift_buttons = r.invitation(True)
        self.assertNotIn("۱۰۰", plain)
        self.assertIn("۱۰۰", gift)
        self.assertIn("۲۴", gift)
        self.assertIn("هر دو", gift)
        self.assertIn("قرعه", plain)
        self.assertNotIn("آیپی ثابت", gift)
        self.assertIn("freecfg_welcome_100", str(gift_buttons))
        self.assertNotIn("freecfg_welcome_100", str(buttons))
        self.assertIn("channel_" + r.CODE, str(buttons))
        self.assertTrue(
            all(
                b.get("url", "").startswith("https://t.me/")
                for row in buttons["inline_keyboard"]
                for b in row
            )
        )

    def test_only_exact_private_delivery_receipt_is_success(self):
        body, markup = r.invitation(False)
        result = {
            "ok": True,
            "result": {
                "chat": {"type": "private", "id": 123},
                "text": body,
                "reply_markup": markup,
                "message_id": 12,
                "date": 1791300000,
            },
        }
        self.assertTrue(r.verified_send_receipt(result, 123, body, markup))
        for key, value in [
            ("chat", {"type": "channel", "id": -1003855234264}),
            ("text", "changed"),
            ("reply_markup", {}),
            ("message_id", 0),
            ("date", None),
        ]:
            response = copy.deepcopy(result)
            response["result"][key] = value
            self.assertFalse(r.verified_send_receipt(response, 123, body, markup), key)

    def test_purchase_during_rate_delay_prevents_send(self):
        decisions = iter([True, False])
        calls = []
        self.assertEqual(
            r.deliver_once(
                reserve=lambda: True,
                fresh=lambda: next(decisions),
                send=lambda: calls.append("send"),
                finish=lambda status, response: calls.append(status),
                gate=r.RateGate(),
            ),
            "audience_blocked",
        )
        self.assertEqual(calls, ["audience_blocked"])

    def test_current_panel_state_requires_exact_identity_quota_and_unexpired_usage(self):
        service = proof(1)["payload"]["service"]
        state = {
            "exists": True,
            "enabled": True,
            "expired": False,
            "quota_exhausted": False,
            "client_id": "offline",
            "sub_id": "offline",
            "traffic_limit_bytes": 1024**3,
            "traffic_used_bytes": 500,
            "expiry_ts_ms": int((NOW + timedelta(hours=1)).timestamp() * 1000),
        }
        self.assertTrue(r.panel_ready(service, state, NOW))
        for key, value in [
            ("client_id", "other"),
            ("sub_id", None),
            ("traffic_limit_bytes", None),
            ("traffic_used_bytes", 1024**3),
            ("expiry_ts_ms", int(NOW.timestamp() * 1000)),
            ("expired", True),
            ("quota_exhausted", True),
            ("enabled", False),
            ("exists", False),
        ]:
            bad = {**state, key: value}
            self.assertFalse(r.panel_ready(service, bad, NOW), key)

    def test_initial_health_proof_must_be_at_most_thirty_minutes_old(self):
        rows = [proof(1), proof(2)]
        rows[1]["payload"]["checked_at_utc"] = (NOW - timedelta(minutes=31)).isoformat()
        self.assertFalse(r.shared_ready(rows, BINDING, NOW, max_health_age=timedelta(minutes=30)))

    def test_owned_panel_cache_limits_reads_and_fails_on_missing_quota(self):
        clock = [0.0]
        calls = []

        class Backend:
            invalid = False

            def service_state(self, ref):
                calls.append(ref)
                return {
                    "exists": True,
                    "enabled": True,
                    "expired": False,
                    "quota_exhausted": False,
                    "client_id": "offline",
                    "sub_id": "offline",
                    "traffic_limit_bytes": 1024**3,
                    "traffic_used_bytes": None if self.invalid else 500,
                    "expiry_ts_ms": int((NOW + timedelta(hours=1)).timestamp() * 1000),
                }

        backend = Backend()
        cache = r.OwnedPanelCache(clock=lambda: clock[0])
        rows = [proof(1), proof(2)]
        for _ in range(5):
            self.assertTrue(cache.verify(rows, backend, NOW))
        self.assertEqual(len(calls), 2)
        clock[0] = 29.9
        self.assertTrue(cache.verify(rows, backend, NOW))
        self.assertEqual(len(calls), 2)
        clock[0] = 30
        backend.invalid = True
        self.assertFalse(cache.verify(rows, backend, NOW))
        self.assertEqual(len(calls), 3)
        self.assertFalse(cache.verify(rows, backend, NOW))
        self.assertEqual(len(calls), 3)

    def test_durable_sql_reservation_is_visible_before_effect_and_blocks_replay(self):
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool

        from pv_growth.database.models import MessageLog, User
        from pv_growth.database.types import Base

        engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(User(telegram_user_id=123))
            db.commit()
            user_id = db.scalar(select(User.id))
        calls = []

        def reserve():
            with Session(engine) as db:
                if db.scalar(select(MessageLog.id).where(MessageLog.dedupe_key == "offline:once")):
                    return False
                db.add(
                    MessageLog(
                        user_id=user_id,
                        template_code="offline",
                        purpose="offline",
                        dedupe_key="offline:once",
                        status="reserved",
                        attempts=1,
                    )
                )
                db.commit()
                return True

        def fresh():
            with Session(engine) as db:
                self.assertEqual(db.scalar(select(MessageLog.status)), "reserved")
            return True

        def send():
            calls.append("api_effect")
            return {"ok": False, "delivery_unknown": True}

        def finish(status, response):
            with Session(engine) as db:
                row = db.scalar(select(MessageLog))
                row.status = status
                db.commit()

        kwargs = dict(reserve=reserve, fresh=fresh, send=send, finish=finish, gate=r.RateGate())
        self.assertEqual(r.deliver_once(**kwargs), "delivery_unknown")
        self.assertEqual(r.deliver_once(**kwargs), "dedupe_skip")
        self.assertEqual(calls, ["api_effect"])
        with Session(engine) as db:
            self.assertEqual(db.scalar(select(MessageLog.status)), "delivery_unknown")
        engine.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)
