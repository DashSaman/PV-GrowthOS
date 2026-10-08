"""Bounded native invitation job; reservations survive lost delivery receipts."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from pv_growth.acquisition.entry import latest_shared_context
from pv_growth.database.models import AppConfig, FreeAllocation, MessageLog, PublishedPost, User
from pv_growth.database.types import utcnow
from pv_growth.events.service import get_or_create_user
from pv_growth.free_config.audience import evaluate_history
from pv_growth.free_config.budgets import GIB
from pv_growth.free_config.owned_delivery import require_client_identity
from pv_growth.free_config.plan_binding import require_tunnel_plan
from pv_growth.free_config.shared_public import local_day
from pv_growth.jobs.runner import handler
from pv_growth.lifecycle.service import marketing_cooldown
from pv_growth.messaging.service import record_send
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader
from pv_growth.provisioning.xui import client_email_for, get_provisioning


def enabled_policy(session):
    row = session.get(AppConfig, "pv_acquisition_policy", populate_existing=True)
    value = row.value if row else None
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return None
    cap = value.get("daily_cap")
    return value if type(cap) is int and 1 <= cap <= 100 else None


def previously_invited(session, user_id):
    return (
        session.scalar(
            select(MessageLog.id)
            .where(
                MessageLog.user_id == user_id,
                or_(
                    MessageLog.purpose.startswith("bot45_"),
                    MessageLog.purpose.startswith("never_buyer_invite_"),
                    MessageLog.purpose.startswith("pv_growth_invite_"),
                ),
            )
            .limit(1)
        )
        is not None
    )


def private_chat(chat, recipient):
    return (
        isinstance(chat, dict)
        and chat.get("type") == "private"
        and type(chat.get("id")) is int
        and chat["id"] == recipient
    )


def verified_receipt(result, recipient, body, markup):
    return bool(
        isinstance(result, dict)
        and private_chat(result.get("chat"), recipient)
        and type(result.get("message_id")) is int
        and result["message_id"] > 0
        and type(result.get("date")) is int
        and result.get("text") == body
        and result.get("reply_markup") == markup
    )


class BotFailure(Exception):
    def __init__(self, code=None):
        super().__init__("purchase bot request failed")
        self.code = code


class DispatchBlocked(Exception):
    pass


def dispatch_window():
    now = utcnow()
    return 6 * 60 + 15 <= now.hour * 60 + now.minute < 6 * 60 + 30


class PurchaseBot:
    """Single worker rate <=2 requests/sec. No exception exposes token or URL."""

    def __init__(self, token):
        self.token = token
        self.next_at = 0.0
        self.client = httpx.Client(timeout=15, follow_redirects=False)

    def call(self, method, payload, *, guard=None):
        delay = max(0, self.next_at - time.monotonic())
        if delay:
            time.sleep(delay)
        if guard is not None:
            guard()  # after pacing, directly before external I/O
        self.next_at = time.monotonic() + 0.5
        try:
            response = self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            body = response.json()
        except Exception:
            raise BotFailure() from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            code = body.get("error_code") if isinstance(body, dict) else None
            raise BotFailure(code if type(code) is int else None)
        return body.get("result")

    def close(self):
        self.client.close()


def two_owned_posts(session, settings, adapter):
    """Current panel truth for two independently receipted, verified publications."""
    if latest_shared_context(session, settings) is None:
        return False
    try:
        binding = require_tunnel_plan(session, settings)
        now = utcnow()
        rows = session.execute(
            select(FreeAllocation, PublishedPost)
            .join(PublishedPost, PublishedPost.dedupe_key == FreeAllocation.id)
            .where(
                FreeAllocation.bucket == "public",
                FreeAllocation.status == "published",
                FreeAllocation.expires_at > now,
                FreeAllocation.traffic_bytes == GIB,
                PublishedPost.kind == "pv_shared",
                PublishedPost.message_id > 0,
                PublishedPost.channel_id == str(settings.free_channel_id),
            )
            .order_by(FreeAllocation.created_at.desc())
            .limit(10)
        )
        valid_refs, messages = set(), set()
        for row, post in rows:
            payload = row.payload
            if not isinstance(payload, dict) or row.service_ref != client_email_for(row.id):
                continue
            if payload.get("tunnel_plan") != binding or payload.get("health_method") != "https_via_proxy":
                continue
            try:
                checked = datetime.fromisoformat(payload["checked_at_utc"])
                checked = checked.replace(tzinfo=UTC) if checked.tzinfo is None else checked.astimezone(UTC)
            except (ValueError, TypeError, KeyError):
                continue
            if not now.replace(tzinfo=UTC) - timedelta(hours=24) <= checked <= now.replace(tzinfo=UTC):
                continue
            service = payload.get("service", {})
            if not isinstance(service, dict) or service.get("service_ref") != row.service_ref:
                continue
            state = adapter.service_state(row.service_ref)
            require_client_identity(service, state)
            if (
                state.get("exists") is not True
                or state.get("enabled") is not True
                or state.get("expired") is not False
                or state.get("quota_exhausted") is not False
                or state.get("traffic_limit_bytes") != GIB
                or type(state.get("traffic_used_bytes")) is not int
                or not 0 <= state["traffic_used_bytes"] < GIB
                or state.get("expiry_ts_ms") != service.get("expiry_ts_ms")
                or type(state.get("expiry_ts_ms")) is not int
                or state["expiry_ts_ms"] <= now.replace(tzinfo=UTC).timestamp() * 1000
            ):
                continue
            valid_refs.add(row.service_ref)
            messages.add(post.message_id)
            if len(valid_refs) >= 2 and len(messages) >= 2:
                return True
    except Exception:
        return False
    return False


def invitation(day):
    body = (
        "🎁 تست سرویس اصلی PV Network را امتحان کن\n\n"
        "📢 در کانال رایگان، تست مشترک تازه و قرعه‌کشی روزانهٔ تست شخصی داریم.\n"
        "✅ اتصال HTTPS تست‌ها پیش از انتشار از سرور ما بررسی می‌شود؛ "
        "نتیجه روی اینترنت تو ممکن است متفاوت باشد.\n"
        "🔐 دریافت با عضویت در هر دو کانال، ۴۵ روز بدون خرید و نداشتن سرویس فعال است.\n\n"
        "💎 برای سرویس شخصی با حجم و مدت انتخابی، پلن‌ها و شرایط را در ربات رسمی ببین.\n"
        "🛒 خرید و تعرفه‌ها: @pvnetwork_bot"
    )
    markup = {
        "inline_keyboard": [
            [
                {
                    "text": "📥 بررسی تازه‌ترین تست",
                    "url": f"https://t.me/pvgrowthos_bot?start=channel_acq_invite_{day.replace('-', '')}",
                }
            ],
            [{"text": "🎁 کانال رایگان", "url": "https://t.me/pvnetwork_freeconfig"}],
            [
                {
                    "text": "🎲 ثبت‌نام قرعه‌کشی",
                    "url": "https://t.me/pvgrowthos_bot?start=freecfg_pv_daily_lottery",
                }
            ],
            [{"text": "🛒 خرید سرویس شخصی", "url": "https://t.me/pvnetwork_bot"}],
        ]
    }
    return body, markup


def run_daily(session, settings, day, *, reader=None, bot=None, adapter=None):
    policy = enabled_policy(session)
    if policy is None or day != local_day().isoformat() or not settings.purchase_bot_token:
        return {"status": "disabled"}
    if not dispatch_window():
        return {"status": "outside_window"}
    adapter = adapter or get_provisioning(settings)
    if not two_owned_posts(session, settings, adapter):
        return {"status": "offers_unavailable"}
    reader = reader or MirzaMySQLReader(settings)
    owned_bot = bot is None
    bot = bot or PurchaseBot(settings.purchase_bot_token)
    try:
        identity = bot.call("getMe", {})
        if (
            not isinstance(identity, dict)
            or identity.get("username") != "pvnetwork_bot"
            or identity.get("is_bot") is not True
            or str(identity.get("id")) != settings.purchase_bot_token.split(":", 1)[0]
        ):
            return {"status": "identity_mismatch"}
        key = f"pv_acquisition_day:{day}"
        if session.get(AppConfig, key) is not None:
            return {"status": "already_attempted"}
        try:
            with session.begin_nested():
                session.add(AppConfig(key=key, value={"status": "reserved", "cap": policy["daily_cap"]}))
                session.flush()
        except IntegrityError:
            return {"status": "already_attempted"}
        session.commit()  # a lost process cannot resume the daily cohort
        summary = {"status": "finished", "attempted": 0, "sent": 0, "blocked": 0, "unknown": 0}
        body, markup = invitation(day)
        started = time.monotonic()
        proof_at = started
        for recipient in reader.list_private_invite_contacts(limit=5000):
            if (
                summary["attempted"] >= policy["daily_cap"]
                or time.monotonic() - started > 120
                or day != local_day().isoformat()
                or not dispatch_window()
            ):
                break
            if enabled_policy(session) is None:
                summary["status"] = "policy_disabled"
                break
            user, _ = get_or_create_user(session, telegram_user_id=recipient, telegram_chat_id=recipient)
            if (
                user.is_blocked
                or previously_invited(session, user.id)
                or marketing_cooldown(session, user.id, 168)
            ):
                continue
            if time.monotonic() - proof_at >= 30:
                if not two_owned_posts(session, settings, adapter):
                    summary["status"] = "offers_unavailable"
                    break
                proof_at = time.monotonic()
            history = reader.fetch_private_invite_history(recipient)
            if history is None:
                continue
            decision = evaluate_history(*history, now=utcnow(), dormant_days=45)
            if not decision.eligible or decision.reason != "never_purchased":
                continue
            try:
                chat = bot.call("getChat", {"chat_id": recipient})
                if not private_chat(chat, recipient):
                    continue
            except BotFailure as exc:
                if exc.code == 403:
                    user.is_blocked = True
                    session.commit()
                    continue
                summary["status"] = "api_stopped"
                break
            # Same user lock as lifecycle dispatch: check -> durable reservation
            # is indivisible across purposes, then commit releases the lock.
            user = session.scalar(
                select(User)
                .where(User.id == user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                user.is_blocked
                or previously_invited(session, user.id)
                or marketing_cooldown(session, user.id, 168)
            ):
                session.commit()
                continue
            # Forever dedupe spans every daily campaign, not just today's key.
            row, created = record_send(
                session,
                user_id=user.id,
                template_code="pv_growth_invitation",
                purpose=f"pv_growth_invite_{day.replace('-', '')}",
                dedupe_key=f"pv_growth_invite_once:{user.id}",
            )
            if not created:
                continue
            row.attempts += 1
            row.meta = {
                "reserved_at_utc": utcnow().isoformat(),
                "source": f"channel:acq_invite_{day.replace('-', '')}",
            }
            session.commit()
            summary["attempted"] += 1

            def final_guard(recipient=recipient):
                if not two_owned_posts(session, settings, adapter):
                    raise DispatchBlocked("offers_unavailable")
                # Complete current three-table history after pacing/reservation.
                history = reader.fetch_private_invite_history(recipient)
                decision = (
                    evaluate_history(*history, now=utcnow(), dormant_days=45) if history is not None else None
                )
                if decision is None or not decision.eligible or decision.reason != "never_purchased":
                    raise DispatchBlocked("audience_blocked")
                current_policy = enabled_policy(session)
                if current_policy is None or summary["attempted"] > current_policy["daily_cap"]:
                    raise DispatchBlocked("policy_disabled")
                if (
                    day != local_day().isoformat()
                    or not dispatch_window()
                    or time.monotonic() - started > 120
                ):
                    raise DispatchBlocked("outside_window")

            try:
                result = bot.call(
                    "sendMessage",
                    {"chat_id": recipient, "text": body, "reply_markup": markup},
                    guard=final_guard,
                )
                if not verified_receipt(result, recipient, body, markup):
                    raise BotFailure()
                row.status = "sent"
                row.sent_at = utcnow()
                row.meta = {**row.meta, "message_id": result["message_id"], "receipt_verified": True}
                summary["sent"] += 1
            except DispatchBlocked as exc:
                row.status = "audience_blocked" if str(exc) == "audience_blocked" else "gate_blocked"
                if row.status == "gate_blocked":
                    summary["status"] = str(exc)
            except BotFailure as exc:
                row.status = "failed" if exc.code is not None else "delivery_unknown"
                row.sent_at = utcnow()
                row.meta = {**row.meta, "error_code": exc.code}
                if exc.code == 403:
                    user.is_blocked = True
                    summary["blocked"] += 1
                else:
                    summary["unknown"] += 1
                    summary["status"] = "api_stopped"
            session.commit()
            if summary["status"] != "finished":
                break
        session.get(AppConfig, key).value = summary
        session.commit()
        return summary
    finally:
        if owned_bot:
            bot.close()


@handler("acquisition.daily_invite")
def daily_invite_job(session, settings, payload):
    run_daily(session, settings, payload.get("day"))
