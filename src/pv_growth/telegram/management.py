"""Private creator-only bounded GrowthOS controls and native daily receipts."""

import re
from datetime import timedelta

from sqlalchemy import func, select

from pv_growth.core.flags import FlagService
from pv_growth.database.models import AppConfig, FreeAllocation, MessageLog, PublishedPost
from pv_growth.database.types import utcnow
from pv_growth.free_config.autonomous import hours_for
from pv_growth.free_config.membership import FREE_CHANNEL_ID, MAIN_CHANNEL_ID
from pv_growth.free_config.shared_public import growth_policy, local_day
from pv_growth.free_config.web_publication import FREE_CHANNEL_ALIASES, day_window
from pv_growth.jobs.runner import handler
from pv_growth.telegram.client import InlineKeyboard


def authorized(settings, client, user_id):
    if type(user_id) is not int or user_id <= 0:
        return False
    try:
        memberships = [
            client.get_chat_member(channel, user_id) for channel in (MAIN_CHANNEL_ID, FREE_CHANNEL_ID)
        ]
        return all(
            member.get("status") in {"creator", "administrator"}
            and type(member.get("user", {}).get("id")) is int
            and member["user"]["id"] == user_id
            for member in memberships
        ) and any(member.get("status") == "creator" for member in memberships)
    except Exception:
        return False


def keyboard():
    return InlineKeyboard(
        [
            [{"text": "📊 وضعیت و گزارش", "callback_data": "manage:status"}],
            [{"text": "📈 فروش عادی و همکاری", "callback_data": "manage:sales"}],
            [{"text": "🔎 تحقیق و پیشنهاد رشد", "callback_data": "manage:research"}],
            [
                {"text": "تحقیق روزانه روشن", "callback_data": "manage:research:on"},
                {"text": "توقف تحقیق", "callback_data": "manage:research:off"},
            ],
            [
                {"text": "🌐 روزانه ۱۰ وب", "callback_data": "manage:web:10"},
                {"text": "⏸ توقف وب", "callback_data": "manage:web:off"},
            ],
            [
                {"text": "💎 تست اصلی روشن", "callback_data": "manage:shared:on"},
                {"text": "⏸ توقف تست اصلی", "callback_data": "manage:shared:off"},
            ],
            [
                {"text": "حداکثر ۱ تست فعال", "callback_data": "manage:active:1"},
                {"text": "حداکثر ۲ تست فعال", "callback_data": "manage:active:2"},
            ],
            [
                {"text": "🎁 هدیه هفتگی روشن", "callback_data": "manage:gift:on"},
                {"text": "⏸ توقف هدیه هفتگی", "callback_data": "manage:gift:off"},
            ],
            [{"text": "🕒 راهنمای ساعت‌ها", "callback_data": "manage:hours_help"}],
        ]
    )


def status_text(session, settings):
    day = local_day(utcnow())
    start, end = day_window(day)
    policy = growth_policy(session)
    posts = dict(
        session.execute(
            select(PublishedPost.kind, func.count())
            .where(
                PublishedPost.channel_id.in_(FREE_CHANNEL_ALIASES),
                PublishedPost.message_id > 0,
                PublishedPost.posted_at >= start,
                PublishedPost.posted_at < end,
            )
            .group_by(PublishedPost.kind)
        ).all()
    )
    intro = dict(
        session.execute(
            select(FreeAllocation.status, func.count())
            .where(FreeAllocation.bucket == "acq_intro")
            .group_by(FreeAllocation.status)
        ).all()
    )
    receipts = (
        session.scalar(
            select(func.count())
            .select_from(MessageLog)
            .where(MessageLog.purpose == "pv_intro_5g", MessageLog.status == "sent")
        )
        or 0
    )
    weekly = (
        session.scalar(
            select(func.count())
            .select_from(FreeAllocation)
            .where(
                FreeAllocation.day == day,
                FreeAllocation.bucket == "lottery",
                FreeAllocation.status == "delivered",
                FreeAllocation.payload["grant_mode"].as_string() == "weekly_member_gift",
            )
        )
        or 0
    )
    probe = session.get(AppConfig, f"web-probe-budget:{day}")
    checked = probe.value.get("attempts", 0) if probe else 0
    hours = sorted(f"{(h + 3) % 24:02}:30" for h in hours_for(session, day))
    flags = FlagService(settings)
    gates = all(
        flags.enabled(k)
        for k in ("FREE_CONFIG_ENABLED", "PUBLIC_CONFIG_ENABLED", "PV_EXCLUSIVE_CONFIG_ENABLED")
    )

    def label(value):
        return "روشن" if value else "خاموش"

    return (
        f"⚙️ مدیریت PV GrowthOS · {day}\n\n"
        f"🐳 اجرای زمان‌بندی روی سرور: {label(settings.scheduler_enabled)}\n"
        f"🌐 وب: {label(policy.get('community_enabled'))} · "
        f"هدف {policy.get('community_daily_slots', 10)} در روز\n"
        f"💎 تست اصلی: {label(policy.get('public_shared_enabled'))} · "
        f"حداکثر {policy.get('owned_shared_active_limit', 2)} فعال\n"
        f"🎁 هدیه هفتگی: {label(policy.get('weekly_member_gift_enabled'))}\n"
        f"🔒 پرچم‌های لازم: {label(gates)}\n"
        f"🕒 ساعت‌های تهران: {', '.join(hours)}\n\n"
        f"📨 رسید نشر امروز: {posts.get('free_public', 0)} وب، {posts.get('pv_shared', 0)} تست اصلی\n"
        f"🎁 تحویل هدیه هفتگی امروز: {weekly}\n"
        f"📥 اشانتیون اولیه دارای رسید: {receipts}\n"
        f"⛔ رد قطعی تلگرام: {intro.get('delivery_rejected', 0)}\n"
        f"⏳ ساخت/ارسال نامعلوم: {intro.get('provision_unknown', 0) + intro.get('delivery_unknown', 0)}\n"
        f"🔎 بررسی کاندیدای وب امروز: {checked}\n\n"
        "تست بعدی فقط پس از آزادشدن ظرفیت و در موعد برنامه منتشر می‌شود. "
        "وب ناسالم منتشر نمی‌شود؛ کمبود منبع می‌تواند تعداد واقعی را کمتر از هدف کند. "
        "رزرو یا نتیجه نامعلوم تحویل نیست و تکرار نمی‌شود. تعداد پیام اثبات رشد فروش نیست.\n\n"
        "ساعت‌ها: /hours 01:30,07:30,09:30,11:30,13:30,15:30,17:30,19:30,21:30,23:30\n"
        "تعداد وب: /web_count 10 · سقف فعال: /active 2 · مدیریت: /manage"
    )


def parse_hours(raw):
    values = raw.split(",")
    if not 1 <= len(values) <= 10 or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):30", v) for v in values):
        raise ValueError("hours")
    hours = sorted(int(v[:2]) for v in values)
    if len(set(hours)) != len(hours) or (
        len(hours) > 1 and min((hours[(i + 1) % len(hours)] - h) % 24 for i, h in enumerate(hours)) < 2
    ):
        raise ValueError("hours")
    return sorted((h - 3) % 24 for h in hours)


def handle_action(session, settings, client, *, user_id, chat_id, chat_type, action, request_key=None):
    if type(chat_id) is not int or chat_id != user_id or chat_type != "private":
        return True
    if not authorized(settings, client, user_id):
        client.send_message(
            chat_id, "این بخش فقط برای مالک تأییدشدهٔ یکی از کانال‌های PV با دسترسی مدیریت هر دو کانال است."
        )
        return True
    if request_key is not None:
        if not re.fullmatch(r"[mc]:[0-9a-f]{40}", request_key):
            return True
        key = "bot-control:" + request_key
        if session.get(AppConfig, key) is not None:
            client.send_message(
                chat_id, "این دستور قبلاً ثبت شده است.\n\n" + status_text(session, settings), keyboard()
            )
            return True
        session.add(AppConfig(key=key, value={"processed_at_utc": utcnow().isoformat()}))
        session.flush()
    if action in {"research", "research:on", "research:off"}:
        from pv_growth.growth_research import report_text

        paused = session.get(AppConfig, "pv_free_growth_policy")
        if action != "research" and paused and paused.value.get("operational_pause") is True:
            session.commit()
            client.send_message(chat_id, "تغییر تنظیم تحقیق تا پایان نصب متوقف است.", keyboard())
            return True
        if action != "research":
            control = session.get(AppConfig, "growth_research_control")
            value = {"enabled": action == "research:on"}
            if control:
                control.value = value
            else:
                session.add(AppConfig(key="growth_research_control", value=value))
        session.commit()
        control = session.get(AppConfig, "growth_research_control")
        state = "خاموش" if control and control.value.get("enabled") is False else "روشن"
        client.send_message(chat_id, f"تحقیق روزانه: {state}\n\n" + report_text(session), keyboard())
        return True
    if action == "sales":
        from pv_growth.mirza_adapter.retail_sales import collect_report, render_report

        cutoff = utcnow().replace(minute=0, second=0, microsecond=0)
        report = collect_report(session, settings, key=f"retail-sales:hour:{cutoff:%Y%m%d%H}", end=cutoff)
        session.commit()
        client.send_message(chat_id, render_report(report), keyboard())
        return True
    locked_policy = session.scalar(
        select(AppConfig)
        .where(AppConfig.key == "pv_free_growth_policy")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    policy = dict(locked_policy.value) if locked_policy and isinstance(locked_policy.value, dict) else {}
    if policy.get("operational_pause") is True and action not in ("menu", "status"):
        session.commit()
        client.send_message(
            chat_id,
            "انتشار برای نصب نسخه به‌طور موقت متوقف است؛ تنظیم پس از اتمام نصب قابل تغییر است.",
            keyboard(),
        )
        return True
    changed = False
    notice = ""
    if action in ("menu", "status"):
        manager = session.get(AppConfig, "pv_bot_manager")
        value = {"chat_id": chat_id, "reports_enabled": True}
        if manager:
            manager.value = value
        else:
            session.add(AppConfig(key="pv_bot_manager", value=value))
        session.commit()
    elif action == "hours_help":
        notice = (
            "ساعت‌ها را با /hours و زمان‌های HH:30 بفرست؛ فاصله حداقل دو ساعت. "
            "تغییر از فردای تهران اعمال می‌شود.\n\n"
        )
    elif action in ("shared:on", "shared:off", "gift:on", "gift:off", "web:off", "active:1", "active:2"):
        field, value = {
            "shared:on": ("public_shared_enabled", True),
            "shared:off": ("public_shared_enabled", False),
            "gift:on": ("weekly_member_gift_enabled", True),
            "gift:off": ("weekly_member_gift_enabled", False),
            "web:off": ("community_enabled", False),
            "active:1": ("owned_shared_active_limit", 1),
            "active:2": ("owned_shared_active_limit", 2),
        }[action]
        policy = {**policy, field: value}
        changed = True
    elif re.fullmatch(r"web:(?:[1-9]|10)", action):
        policy = {
            **policy,
            "community_daily_slots": int(action.split(":")[1]),
            "community_enabled": True,
            "community_direct_enabled": True,
        }
        changed = True
    elif action.startswith("hours:"):
        try:
            hours = parse_hours(action[6:])
        except ValueError:
            notice = "ساعت نامعتبر است؛ فقط HH:30، حداکثر ده موعد و فاصله حداقل دو ساعت.\n\n"
        else:
            day = local_day(utcnow())
            policy = {
                **policy,
                "public_hours_utc": hours_for(session, day),
                "pending_public_hours_utc": hours,
                "public_schedule_effective_day": (day + timedelta(days=1)).isoformat(),
            }
            changed = True
            notice = "ساعت‌های جدید از فردای تهران اعمال می‌شوند؛ موعدهای امروز بازپخش نمی‌شوند.\n\n"
    else:
        notice = "این دستور مدیریتی مجاز نیست؛ بودجه، مقصد اصلی و ارسال‌های قبلی تغییر نمی‌کنند.\n\n"
    if changed:
        policy = {
            **policy,
            "owned_shared_channel": "@pvnetwork_freeconfig",
            "owned_main_direct_enabled": False,
            "owned_shared_direct_enabled": True,
            "lottery_enabled": False,
        }
        row = session.get(AppConfig, "pv_free_growth_policy")
        if row:
            row.value = policy
        else:
            session.add(AppConfig(key="pv_free_growth_policy", value=policy))
        session.commit()
        notice = notice or "✅ تنظیم ثبت شد؛ سقف‌ها و رسیدهای قبلی حفظ شده‌اند.\n\n"
    session.commit()  # policy and control receipt are atomic, including invalid/no-op actions
    client.send_message(chat_id, notice + status_text(session, settings), keyboard())
    return True


@handler("runtime.daily_report")
def daily_report(session, settings, payload):
    from pv_growth.events.service import get_or_create_user
    from pv_growth.telegram.client import get_telegram

    day = local_day(utcnow()).isoformat()
    if payload.get("day") != day:
        return
    from datetime import datetime

    from pv_growth.growth_research import nightly_summary
    from pv_growth.mirza_adapter.retail_sales import collect_report, render_report

    cutoff = datetime.fromisoformat(day).replace(hour=20, minute=25)
    if utcnow() < cutoff:
        return
    sales = collect_report(session, settings, key=f"retail-sales:daily:{day}", end=cutoff)
    manager = session.get(AppConfig, "pv_bot_manager")
    if not manager or manager.value.get("reports_enabled") is not True:
        return
    chat = manager.value.get("chat_id")
    client = get_telegram(settings)
    if not authorized(settings, client, chat):
        return
    user, _ = get_or_create_user(session, telegram_user_id=chat, telegram_chat_id=chat)
    key = f"native-admin-report:{day}"
    if session.scalar(select(MessageLog.id).where(MessageLog.dedupe_key == key)) is not None:
        return
    from pv_growth.messaging.service import record_send

    receipt, created = record_send(
        session,
        user_id=user.id,
        template_code="native_admin_report",
        purpose="native_admin_report",
        dedupe_key=key,
    )
    if not created:
        return
    receipt.attempts = 1
    session.commit()
    try:
        result = client.send_message(
            chat,
            status_text(session, settings)
            + "\n\n"
            + render_report(sales)
            + "\n\n"
            + nightly_summary(session, day),
            keyboard(),
        )
        confirmed = (
            type(result.get("message_id")) is int
            and result["message_id"] > 0
            and type(result.get("chat", {}).get("id")) is int
            and result["chat"]["id"] == chat
        )
    except Exception:
        confirmed = False
    receipt.status = "sent" if confirmed else "delivery_unknown"
    receipt.sent_at = utcnow() if confirmed else None
    session.commit()
