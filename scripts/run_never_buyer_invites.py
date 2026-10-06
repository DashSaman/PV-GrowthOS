"""Authorized one-shot private invitations; explicit --execute required.

The host wrapper protects the existing main-bot config checksum and passes its
token in memory to the healthy GrowthOS container. No token/IDs/URI/DSN printed.
A campaign reservation makes interruption non-resumable; ambiguous deliveries
are never retried. Only the two files' offline checks are run during preparation.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

CODE = "never_buyer_invite_20261006"
STATE = "never_buyer_invite_20261006_state"
PROTECTED_SHA = "234bcf26ba6c89643e729a380ff35ed81805cdd527a2f693dbbfdba7bc633a6c"
GIFT_DAY = "2026-10-06"
FREE_CHANNEL = "-1004310246787"
TEHRAN = timezone(timedelta(hours=3, minutes=30))
WORKERS = 2
GIB = 1024**3
MIB = 1024**2
TUNNEL_IDS = [17, 21, 37, 82, 83, 125, 126, 127, 128]


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("timestamp unavailable")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def never_buyer(decision):
    return decision.eligible is True and decision.reason == "never_purchased"


def private_receipt(chat, recipient):
    return (
        type(recipient) is int
        and 0 < recipient < 2**52
        and isinstance(chat, dict)
        and chat.get("type") == "private"
        and type(chat.get("id")) is int
        and chat["id"] == recipient
    )


def verified_send_receipt(response, recipient, body, markup):
    receipt = response.get("result", {})
    return bool(
        response.get("ok") is True
        and isinstance(receipt, dict)
        and private_receipt(receipt.get("chat"), recipient)
        and type(receipt.get("message_id")) is int
        and receipt["message_id"] > 0
        and receipt.get("text") == body
        and receipt.get("reply_markup") == markup
        and type(receipt.get("date")) is int
    )


def shared_ready(rows, binding, now, *, max_health_age=timedelta(hours=24), minimum=2):
    """Pure validation of durable owned-publication proof, never community."""
    now = utc(now)
    if (
        not isinstance(binding, dict)
        or binding.get("panel_id") != 33
        or binding.get("active") is not True
        or binding.get("inbound_ids") != TUNNEL_IDS
    ):
        return False
    keys, refs, messages = set(), set(), set()
    for row in rows:
        try:
            payload = row["payload"]
            service = payload["service"]
            checked = utc(payload["checked_at_utc"])
            posted = utc(row["posted_at"])
            if (
                row["kind"] != "pv_shared"
                or row["channel_id"] != FREE_CHANNEL
                or type(row["message_id"]) is not int
                or row["message_id"] <= 0
                or not row["key"].startswith("pv_shared:")
                or row["status"] != "published"
                or row["traffic_bytes"] != GIB
                or not now - timedelta(hours=24) <= posted <= now
                or not now - max_health_age <= checked <= now
                or utc(row["expires_at"]) <= now
                or payload.get("tunnel_plan") != binding
                or payload.get("health_method") != "https_via_proxy"
                or not isinstance(payload.get("verified_uri"), str)
                or not payload["verified_uri"].startswith("vless://")
                or row["service_ref"] != "growth-" + hashlib.sha256(row["key"].encode()).hexdigest()[:16]
                or service.get("service_ref") != row["service_ref"]
                or service.get("traffic_bytes") != GIB
                or service.get("enabled") is False
                or not service.get("client_id")
                or not service.get("sub_id")
            ):
                continue
            keys.add(row["key"])
            refs.add(row["service_ref"])
            messages.add(row["message_id"])
        except (KeyError, TypeError, ValueError):
            continue
    return len(keys) >= minimum and len(refs) >= minimum and len(messages) >= minimum


def panel_ready(service, state, now):
    """Current READ-only panel truth must match the durable owned identity."""
    return bool(
        isinstance(service, dict)
        and isinstance(state, dict)
        and state.get("exists") is True
        and state.get("enabled") is True
        and state.get("expired") is False
        and state.get("quota_exhausted") is False
        and isinstance(service.get("client_id"), str)
        and service["client_id"]
        and isinstance(service.get("sub_id"), str)
        and service["sub_id"]
        and state.get("client_id") == service["client_id"]
        and state.get("sub_id") == service["sub_id"]
        and type(state.get("traffic_limit_bytes")) is int
        and state["traffic_limit_bytes"] == service.get("traffic_bytes") == GIB
        and type(state.get("traffic_used_bytes")) is int
        and 0 <= state["traffic_used_bytes"] < GIB
        and type(state.get("expiry_ts_ms")) is int
        and state["expiry_ts_ms"] == service.get("expiry_ts_ms")
        and state["expiry_ts_ms"] > utc(now).timestamp() * 1000
    )


class OwnedPanelCache:
    """At most two owned GET lookups per 30s; ambiguity stops the batch."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.checked_at = None
        self.refs = ()
        self.proofs = {}

    def verify(self, rows, adapter, now):
        by_ref = {row["service_ref"]: row for row in rows}
        with self.lock:
            if self.checked_at is not None and self.clock() - self.checked_at < 30:
                if not all(ref in by_ref for ref in self.refs):
                    return False
                return len(self.refs) == 2 and all(
                    panel_ready(by_ref[ref]["payload"]["service"], self.proofs.get(ref), now)
                    for ref in self.refs
                )
            selected = sorted(rows, key=lambda row: utc(row["posted_at"]), reverse=True)[:2]
            if len({row["service_ref"] for row in selected}) != 2:
                return False
            # Stamp before IO; a failed partial read is never immediately retried.
            self.checked_at = self.clock()
            self.refs = tuple(row["service_ref"] for row in selected)
            self.proofs = {}
            for row in selected:
                try:
                    state = adapter.service_state(row["service_ref"])
                except Exception:
                    return False
                if not panel_ready(row["payload"]["service"], state, now):
                    return False
                self.proofs[row["service_ref"]] = state
            return True


def campaign_active(campaign, now):
    if not isinstance(campaign, dict) or campaign.get("status") != "active":
        return False
    try:
        return (campaign.get("start_at") is None or utc(campaign["start_at"]) <= utc(now)) and (
            campaign.get("end_at") is None or utc(now) < utc(campaign["end_at"])
        )
    except (ValueError, TypeError):
        return False


def gift_ready(policy, campaign, now):
    return bool(
        isinstance(policy, dict)
        and utc(now).astimezone(TEHRAN).date().isoformat() == GIFT_DAY
        and policy.get("gift_enabled") is True
        and policy.get("gift_day") == GIFT_DAY
        and type(policy.get("gift_daily_bytes")) is int
        and policy["gift_daily_bytes"] == 1000 * GIB
        and type(policy.get("gift_traffic_bytes")) is int
        and policy["gift_traffic_bytes"] == 100 * MIB
        and type(policy.get("gift_validity_hours")) is int
        and policy["gift_validity_hours"] == 24
        and campaign_active(campaign, now)
        and campaign.get("code") == "pv_welcome_100"
        and campaign.get("kind") == "free_config_exclusive"
        and isinstance(campaign.get("config"), dict)
        and campaign["config"].get("gift") is True
        and campaign.get("end_at") is not None
        and utc(campaign["end_at"]) <= datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
    )


def invitation(include_gift):
    body = (
        "🎁 کانال رایگان PV Network آماده است!\n\n"
        "🧪 تست‌های مشترک از سرویس اصلی PV؛ اتصال واقعی HTTPS پیش از انتشار "
        "از سرور ما بررسی شده است. نتیجه روی اینترنت شما ممکن است متفاوت باشد.\n"
        "📢 تست‌های تازه را در کانال رایگان ببین.\n"
        "🎲 برای قرعه‌کشی روزانهٔ تست شخصی در ربات ثبت‌نام کن؛ ثبت‌نام به معنی برنده‌شدن نیست.\n"
        "🔐 دریافت رایگان و شرکت در قرعه‌کشی پس از بررسی شرایط و عضویت در هر دو کانال اصلی و رایگان است."
    )
    rows = []
    if include_gift:
        body += (
            "\n\n🎉 هدیهٔ امروز: ۱۰۰ MiB تست شخصی، فقط یک بار؛ "
            "حداکثر ۲۴ ساعت یا تا پایان حجم.\n"
            "بعد از عضویت در هر دو کانال، شرایط دریافت را در ربات بررسی کن؛ "
            "دریافت تابع ظرفیت باقی‌ماندهٔ امروز است."
        )
        rows.append(
            [
                {
                    "text": "🎉 بررسی هدیهٔ ۱۰۰ MiB امروز",
                    "url": "https://t.me/pvgrowthos_bot?start=freecfg_welcome_100",
                }
            ]
        )
    body += "\n\n🚀 برای استفاده روزمره، پلن‌های سرویس اصلی PV را ببین.\n🛒 خرید و تعرفه‌ها: @pvnetwork_bot"
    rows.extend(
        [
            [
                {"text": "🎁 کانال رایگان", "url": "https://t.me/pvnetwork_freeconfig"},
                {
                    "text": "🎲 قرعه‌کشی روزانه",
                    "url": "https://t.me/pvgrowthos_bot?start=freecfg_pv_daily_lottery",
                },
            ],
            [{"text": "🛒 خرید سرویس اصلی PV", "url": "https://t.me/pvnetwork_bot"}],
            [{"text": "📋 راهنمای سرویس PV", "url": "https://t.me/pvgrowthos_bot?start=channel_" + CODE}],
        ]
    )
    return body, {"inline_keyboard": rows}


def log_blocks(log, now):
    """Historical canary excluded forever; recent or unresolved attempts skipped."""
    if any(str(log.get(k, "")).startswith("bot45") for k in ("purpose", "template_code", "dedupe_key")):
        return True
    if log.get("status") in {"reserved", "delivery_unknown"}:
        return True
    meta = log.get("meta") if isinstance(log.get("meta"), dict) else {}
    stamp = log.get("sent_at") or meta.get("reserved_at_utc") or meta.get("sent_at_utc")
    if stamp is None:
        return True  # unknown age cannot establish the authorized cooldown
    try:
        return utc(stamp) >= utc(now) - timedelta(days=7)
    except (ValueError, TypeError):
        return True


class RateGate:
    """One process-wide dispatch clock, <=2 calls/second, plus cohort kill switch."""

    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.next_at = 0.0
        self.reason = None

    def stop(self, reason):
        self.reason = str(reason)
        self.stopped.set()

    def acquire(self, verify=None):
        with self.lock:
            if self.stopped.is_set():
                return False
            delay = max(0.0, self.next_at - self.clock())
            if delay:
                self.sleep(delay)
            if self.stopped.is_set():
                return False
            # A fresh history read can take seconds. Hold the dispatch lock until
            # it completes, then pace from the actual upcoming API dispatch.
            if verify is not None and not verify():
                return False
            if self.stopped.is_set():
                return False
            self.next_at = self.clock() + 0.5
            return True


def deliver_once(*, reserve, fresh, send, finish, gate):
    """Persist first, recheck immediately before effect, never resend ambiguity."""
    if gate.stopped.is_set():
        return "paused"
    if not reserve():
        return "dedupe_skip"
    if not fresh():
        finish("audience_blocked", {})
        return "audience_blocked"
    if not gate.acquire(verify=fresh):
        status = "cancelled" if gate.stopped.is_set() else "audience_blocked"
        finish(status, {})
        return "paused" if gate.stopped.is_set() else "audience_blocked"
    if gate.stopped.is_set():
        finish("cancelled", {})
        return "paused"
    response = send()
    if response.get("error_code") == 429:
        gate.stop("telegram_429")
    elif response.get("error_code") == 401:
        gate.stop("telegram_401")
    if response.get("receipt_verified"):
        status = "sent"
    elif response.get("delivery_unknown") or response.get("ok"):
        status = "delivery_unknown"
    else:
        status = "failed"
    finish(status, response)
    return status


def api_call(token, method, payload, gate):
    if method != "sendMessage" and not gate.acquire():
        return {"ok": False, "paused": True}
    try:
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/" + method,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 — fixed Telegram HTTPS origin
            out = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            out = json.loads(error.read())
        except Exception:
            out = {"ok": False, "error_code": error.code}
        out = {
            "ok": False,
            "error_code": out.get("error_code", error.code),
            "retry_after": out.get("parameters", {}).get("retry_after"),
        }
    except Exception:
        out = {"ok": False, "delivery_unknown": True}
    if out.get("error_code") in (429, 401):
        gate.stop("telegram_" + str(out["error_code"]))
    return out


def run_inside(token):
    """Production-only orchestration. All imports and effects are lazy."""
    from sqlalchemy import select
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError

    from pv_growth.core.config import get_settings
    from pv_growth.core.flags import FlagService
    from pv_growth.database.base import get_session_factory, session_scope
    from pv_growth.database.models import (
        AppConfig,
        Campaign,
        FreeAllocation,
        MessageLog,
        MessageTemplate,
        PublishedPost,
        Source,
        User,
    )
    from pv_growth.database.types import utcnow
    from pv_growth.free_config.audience import evaluate_history
    from pv_growth.free_config.plan_binding import require_tunnel_plan
    from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
    from pv_growth.provisioning.xui import get_provisioning

    settings = get_settings()
    assert settings.env == "production" and make_url(settings.database_url).database == "pv_growth"
    assert make_url(settings.database_url).get_backend_name() == "postgresql"
    assert str(settings.free_channel_id) == FREE_CHANNEL
    gate = RateGate()
    assert api_call(token, "getMe", {}, gate).get("result", {}).get("username") == "pvnetwork_bot"
    assert (
        api_call(settings.telegram_bot_token, "getMe", {}, gate).get("result", {}).get("username")
        == "pvgrowthos_bot"
    )
    reader = MirzaMySQLReader(settings)
    adapter = get_provisioning(settings)
    assert adapter is not None
    panel_cache = OwnedPanelCache()
    get_session_factory(settings)  # initialize shared engine/factory before worker threads
    source_code = "channel:" + CODE

    def campaign_dict(row):
        return (
            None
            if row is None
            else {
                "code": row.code,
                "kind": row.kind,
                "status": row.status,
                "config": row.config,
                "start_at": row.start_at,
                "end_at": row.end_at,
            }
        )

    def ready(db, expected_gift=None, *, initial=False):
        now = datetime.now(UTC)
        assert now.astimezone(TEHRAN).date().isoformat() == GIFT_DAY, "one-day run window ended"
        binding = require_tunnel_plan(db, settings)  # endpoint/inbound policy fingerprint verification
        flags = FlagService(settings)
        assert all(
            flags.enabled(flag)
            for flag in (
                "FREE_CONFIG_ENABLED",
                "PUBLIC_CONFIG_ENABLED",
                "PV_EXCLUSIVE_CONFIG_ENABLED",
            )
        )
        policy_row = db.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
        policy = policy_row.value if policy_row and isinstance(policy_row.value, dict) else {}
        audience_row = db.get(AppConfig, "free_audience_policy", populate_existing=True)
        audience = audience_row.value if audience_row and isinstance(audience_row.value, dict) else {}
        assert audience.get("enabled") is True and audience.get("dormant_days") == 45
        assert audience.get("public_main_promotion") == "forbidden_by_user"
        assert audience.get("bot_invites") == "after_free_channel_ready"
        assert policy.get("public_shared_enabled") is True and policy.get("lottery_enabled") is True
        lottery = db.scalar(
            select(Campaign)
            .where(Campaign.code == "pv_daily_lottery")
            .execution_options(populate_existing=True)
        )
        lottery_data = campaign_dict(lottery)
        assert (
            campaign_active(lottery_data, now)
            and lottery_data["kind"] == "free_config_exclusive"
            and lottery_data["config"].get("lottery") is True
        )
        rows = []
        posts = db.scalars(
            select(PublishedPost)
            .where(
                PublishedPost.channel_id == FREE_CHANNEL,
                PublishedPost.kind == "pv_shared",
                PublishedPost.message_id.is_not(None),
                PublishedPost.posted_at >= utcnow() - timedelta(hours=24),
            )
            .execution_options(populate_existing=True)
        ).all()
        for post in posts:
            allocation = db.get(FreeAllocation, post.dedupe_key, populate_existing=True)
            if allocation is not None:
                rows.append(
                    {
                        "kind": post.kind,
                        "channel_id": post.channel_id,
                        "message_id": post.message_id,
                        "posted_at": post.posted_at,
                        "key": post.dedupe_key,
                        "status": allocation.status,
                        "traffic_bytes": allocation.traffic_bytes,
                        "service_ref": allocation.service_ref,
                        "expires_at": allocation.expires_at,
                        "payload": allocation.payload,
                    }
                )
        max_health_age = timedelta(minutes=30) if initial else timedelta(hours=24)
        assert shared_ready(rows, binding, now, max_health_age=max_health_age), (
            "two verified owned shared posts required"
        )
        candidates = [
            row for row in rows if shared_ready([row], binding, now, max_health_age=max_health_age, minimum=1)
        ]
        assert panel_cache.verify(candidates, adapter, now), "current owned panel proof unavailable"
        gift = db.scalar(
            select(Campaign)
            .where(Campaign.code == "pv_welcome_100")
            .execution_options(populate_existing=True)
        )
        include_gift = gift_ready(policy, campaign_dict(gift), now)
        if expected_gift is True:
            assert include_gift, "gift policy/window changed"
        return include_gift

    def fresh_history(recipient):
        """One complete read-only snapshot of customer state and three histories."""
        with reader.connect() as connection, connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT id,User_Status,agent,step FROM user WHERE id=%s", (recipient,))
            current = cursor.fetchone()
            if (
                not current
                or current["User_Status"] != "Active"
                or current["agent"] != "f"
                or current["step"] != "home"
                or str(current["id"]) != str(recipient)
            ):
                connection.rollback()
                return False
            histories = []
            for statement in (
                "SELECT Status,price_product,time_sell FROM invoice WHERE id_user=%s",
                "SELECT type,status,price,time FROM service_other WHERE id_user=%s",
                "SELECT payment_Status,price,time,at_updated FROM Payment_report WHERE id_user=%s",
            ):
                cursor.execute(statement, (recipient,))
                histories.append(list(cursor.fetchall()))
            connection.rollback()
        return never_buyer(evaluate_history(*histories, now=datetime.now(UTC), dormant_days=45))

    # Initial discovery is complete, SELECT-only, and cannot substitute for fresh checks.
    orders, other, payments = defaultdict(list), defaultdict(list), defaultdict(list)
    with reader.connect() as connection, connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(
            "SELECT id,User_Status,agent,step FROM user "
            "WHERE User_Status='Active' AND agent='f' AND step='home'"
        )
        contacts = list(cursor.fetchall())
        for statement, bucket in (
            ("SELECT id_user,Status,price_product,time_sell FROM invoice", orders),
            ("SELECT id_user,type,status,price,time FROM service_other", other),
            ("SELECT id_user,payment_Status,price,time,at_updated FROM Payment_report", payments),
        ):
            cursor.execute(statement)
            for row in cursor.fetchall():
                identity = str(row.get("id_user", ""))
                if identity.isdecimal():
                    bucket[int(identity)].append(row)
        connection.rollback()
    pool = []
    excluded = Counter()
    now = datetime.now(UTC)
    for contact in contacts:
        raw = str(contact.get("id", ""))
        if not raw.isdecimal() or not 0 < int(raw) < 2**52:
            excluded["invalid_identity"] += 1
            continue
        recipient = int(raw)
        decision = evaluate_history(
            orders[recipient], other[recipient], payments[recipient], now=now, dormant_days=45
        )
        if never_buyer(decision):
            pool.append(recipient)
        else:
            excluded["not_never_buyer"] += 1
    pool = sorted(set(pool), key=lambda value: hashlib.sha256((CODE + str(value)).encode()).hexdigest())

    with session_scope(settings) as db:
        include_gift = ready(db, initial=True)
        assert db.get(AppConfig, STATE) is None, "one-shot already reserved"
        assert db.scalar(select(Campaign.id).where(Campaign.code == CODE)) is None
        assert db.scalar(select(Source.id).where(Source.code == source_code)) is None
        body, markup = invitation(include_gift)
        campaign = Campaign(
            code=CODE,
            name="دعوت خصوصی افراد بدون سابقه خرید",
            kind="purchase",
            status="active",
            start_at=utcnow(),
            end_at=datetime(2026, 10, 6, 20, 30),
            config={
                "never_purchased_only": True,
                "workers": WORKERS,
                "max_dispatch_per_second": 2,
                "cooldown_days": 7,
                "sender_bot": "pvnetwork_bot",
                "private_only": True,
                "no_retries": True,
                "gift_included": include_gift,
            },
        )
        db.add(campaign)
        db.flush()
        db.add(Source(code=source_code, kind="channel", medium="telegram_bot", campaign_id=campaign.id))
        db.add(
            MessageTemplate(
                code=CODE,
                intent="never_buyer_invite",
                locale="fa",
                body=body,
                cta_label="راهنمای سرویس PV",
                cta_url="https://t.me/pvgrowthos_bot?start=channel_" + CODE,
                is_active=0,
            )
        )
        db.add(
            AppConfig(
                key=STATE,
                value={
                    "status": "reserved",
                    "started_at_utc": now.isoformat(),
                    "workers": WORKERS,
                    "max_dispatch_per_second": 2,
                    "pool_count": len(pool),
                    "initial_exclusions": dict(excluded),
                    "source_code": source_code,
                    "no_retries": True,
                    "private_only": True,
                    "gift_included": include_gift,
                    "baseline_source_starts": 0,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                },
            )
        )
        db.commit()  # unique campaign/state reservation blocks a second concurrent invocation

    pending = queue.Queue()
    for recipient in pool:
        pending.put(recipient)
    counts, counts_lock = Counter(), threading.Lock()

    def add_count(reason):
        with counts_lock:
            counts[reason] += 1

    def process(recipient):
        assert type(recipient) is int and recipient > 0
        # Independent GrowthOS DB session per recipient/worker; never share a Session.
        with session_scope(settings) as db:
            try:
                ready(db, include_gift)
            except Exception:
                gate.stop("readiness_changed")
                return "readiness_changed"
            user = db.scalar(
                select(User)
                .where(User.telegram_user_id == recipient)
                .execution_options(populate_existing=True)
            )
            if user is not None:
                if user.is_blocked:
                    return "blocked_skip"
                if user.telegram_chat_id not in (None, recipient):
                    return "invalid_chat_skip"
                if user.mirza_user_ref not in (None, str(recipient)):
                    return "invalid_identity_skip"
                logs = db.scalars(select(MessageLog).where(MessageLog.user_id == user.id)).all()
                if any(
                    log_blocks(
                        {
                            "purpose": log.purpose,
                            "template_code": log.template_code,
                            "dedupe_key": log.dedupe_key,
                            "status": log.status,
                            "sent_at": log.sent_at,
                            "meta": log.meta,
                        },
                        datetime.now(UTC),
                    )
                    for log in logs
                ):
                    return "previous_or_cooldown_skip"
            try:
                if not fresh_history(recipient):
                    return "history_changed_skip"
            except Exception:
                return "history_unavailable_skip"
            # An existing accessible private chat through the main purchase bot is required.
            response = api_call(token, "getChat", {"chat_id": recipient}, gate)
            if not response.get("ok") or not private_receipt(response.get("result"), recipient):
                return "main_bot_private_unavailable_skip"
            if user is None:
                user = User(telegram_user_id=recipient, mirza_user_ref=str(recipient))
                db.add(user)
                try:
                    db.flush()
                except IntegrityError:
                    db.rollback()
                    return "identity_race_skip"
            user_id = user.id
            log_holder = []

            def reserve():
                if gate.stopped.is_set():
                    return False
                # Lock mirrored identity and re-read suppression before reservation.
                locked = db.scalar(
                    select(User)
                    .where(User.id == user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if locked.is_blocked or locked.telegram_chat_id not in (None, recipient):
                    return False
                existing = db.scalars(select(MessageLog).where(MessageLog.user_id == user_id)).all()
                if any(
                    log_blocks(
                        {
                            "purpose": log.purpose,
                            "template_code": log.template_code,
                            "dedupe_key": log.dedupe_key,
                            "status": log.status,
                            "sent_at": log.sent_at,
                            "meta": log.meta,
                        },
                        datetime.now(UTC),
                    )
                    for log in existing
                ):
                    return False
                log = MessageLog(
                    user_id=user_id,
                    template_code=CODE,
                    purpose=CODE,
                    dedupe_key=CODE + ":" + str(user_id),
                    status="reserved",
                    attempts=1,
                    meta={
                        "reserved_at_utc": datetime.now(UTC).isoformat(),
                        "sender_bot": "pvnetwork_bot",
                        "private_only": True,
                        "audience_reason": "never_purchased",
                        "source_code": source_code,
                        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "no_retries": True,
                        "gift_included": include_gift,
                    },
                )
                db.add(log)
                try:
                    db.commit()  # durable before sendMessage; committed identity is not a BOT_STARTED event
                except IntegrityError:
                    db.rollback()
                    return False
                log_holder.append(log.id)
                return True

            def fresh():
                try:
                    ready(db, include_gift)
                    current = db.get(User, user_id, populate_existing=True)
                    if current.is_blocked or current.telegram_chat_id not in (None, recipient):
                        return False
                    # Last operation before API is full read-only authoritative customer history.
                    return fresh_history(recipient)
                except Exception:
                    gate.stop("fresh_verification_unavailable")
                    return False

            def send():
                payload = {
                    "chat_id": recipient,
                    "text": body,
                    "reply_markup": markup,
                    "disable_notification": True,
                }
                response = api_call(token, "sendMessage", payload, gate)
                receipt = response.get("result", {})
                accepted = verified_send_receipt(response, recipient, body, markup)
                if accepted:
                    return {
                        "ok": True,
                        "receipt_verified": True,
                        "message_id": receipt["message_id"],
                        "date": receipt["date"],
                    }
                # Never expose Telegram descriptions/result IDs outside durable internal receipt.
                return {
                    "ok": response.get("ok") is True,
                    "error_code": response.get("error_code"),
                    "delivery_unknown": response.get("delivery_unknown", False),
                    "retry_after": response.get("retry_after"),
                }

            def finish(status, response):
                log = db.get(MessageLog, log_holder[0], populate_existing=True)
                log.status = status
                meta = {
                    **log.meta,
                    "finished_at_utc": datetime.now(UTC).isoformat(),
                    "receipt_verified": response.get("receipt_verified", False),
                    "no_retry": True,
                }
                if status == "sent":
                    log.sent_at = datetime.fromtimestamp(response["date"], UTC).replace(tzinfo=None)
                    meta["telegram_message_id"] = response["message_id"]
                if response.get("error_code") is not None:
                    meta["error_code"] = response["error_code"]
                log.meta = meta
                db.commit()

            return deliver_once(reserve=reserve, fresh=fresh, send=send, finish=finish, gate=gate)

    def worker():
        while not gate.stopped.is_set():
            try:
                recipient = pending.get_nowait()
            except queue.Empty:
                break
            try:
                add_count(process(recipient))
            except Exception:
                # A reserved attempt remains durable/uncertain; no replay after storage outage.
                gate.stop("worker_failure")
                add_count("worker_failure")
            finally:
                pending.task_done()

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(worker) for _ in range(WORKERS)]
        for future in futures:
            future.result()
    with session_scope(settings) as db:
        state = db.get(AppConfig, STATE, populate_existing=True)
        campaign = db.scalar(select(Campaign).where(Campaign.code == CODE))
        campaign.status = "paused" if gate.stopped.is_set() else "finished"
        state.value = {
            **state.value,
            "status": "paused" if gate.stopped.is_set() else "finished",
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "result_counts": dict(counts),
            "remaining_unattempted": pending.qsize(),
            "stop_reason": gate.reason,
            "in_flight_at_stop_may_complete": True,
        }
        db.commit()
        print("NEVER_BUYER_INVITES_JSON " + json.dumps(state.value, ensure_ascii=False))


def host_execute():
    protected = Path("/var/www/mirza_pro/config.php")
    assert hashlib.sha256(protected.read_bytes()).hexdigest() == PROTECTED_SHA
    before = json.loads(
        subprocess.check_output(["/usr/bin/docker", "inspect", "pv-growth-app"], stderr=subprocess.DEVNULL)
    )[0]
    assert before["State"]["Running"] is True and before["State"]["Health"]["Status"] == "healthy"
    assert before["Config"]["Image"].startswith("pv-growth-app:")
    tokens = set(re.findall(r"\b\d{6,12}:[A-Za-z0-9_-]{25,60}\b", protected.read_text()))
    assert len(tokens) == 1
    try:
        # Source stays in the container process memory; no copied secret file.
        source = Path(__file__).read_text(encoding="utf-8")
        result = subprocess.run(  # noqa: S603 — fixed Docker invocation of this reviewed file, no shell
            ["/usr/bin/docker", "exec", "-i", "pv-growth-app", "python", "-c", source, "--inner"],
            input=json.dumps({"token": next(iter(tokens))}),
            text=True,
            capture_output=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("NEVER_BUYER_INVITES_JSON "):
                json.loads(line[len("NEVER_BUYER_INVITES_JSON ") :])
                print(line)
        assert result.returncode == 0, "container runner failed; inspect durable state, never rerun"
    finally:
        assert hashlib.sha256(protected.read_bytes()).hexdigest() == PROTECTED_SHA
        after = json.loads(
            subprocess.check_output(
                ["/usr/bin/docker", "inspect", "pv-growth-app"], stderr=subprocess.DEVNULL
            )
        )[0]
        assert before["Image"] == after["Image"], "container changed during one-shot"
        assert after["State"]["Health"]["Status"] == "healthy"


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--inner"]:
            run_inside(json.load(sys.stdin)["token"])
        elif sys.argv[1:] == ["--execute"]:
            host_execute()
        else:
            print("PREPARATION_ONLY: run offline checks; production requires explicit --execute.")
    except Exception as exc:
        # Never print exception messages/tracebacks, which may embed API tokens or DSNs.
        print(
            "NEVER_BUYER_INVITES_ERROR "
            + json.dumps({"error_type": type(exc).__name__, "retry_allowed": False})
        )
        raise SystemExit(1) from None
