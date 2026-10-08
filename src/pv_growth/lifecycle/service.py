"""Lifecycle automation: scan rules → schedule sends; send with full guard
rails (stop conditions, cooldown, max sends, idempotency). Never spam users:
a purchase kills every inappropriate sales reminder (spec §16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.flags import FlagService
from pv_growth.core.logging import get_logger
from pv_growth.database.models import Event, LifecycleRule, MessageLog, User
from pv_growth.database.types import utcnow
from pv_growth.jobs import service as jobs
from pv_growth.jobs.runner import DeferredJobError, RetryableJobError, handler
from pv_growth.lifecycle.triggers import candidate_users
from pv_growth.messaging import service as messaging
from pv_growth.telegram.client import TelegramRetryableError

log = get_logger("lifecycle")


def _activation_cutoff(conditions: dict):
    if "not_before_utc" not in conditions:
        return None
    try:
        value = datetime.fromisoformat(conditions["not_before_utc"])
        if value.tzinfo is None:
            return False
        return value.astimezone(UTC).replace(tzinfo=None)
    except (TypeError, ValueError):
        return False


def marketing_cooldown(session, user_id: int, hours: int = 24) -> bool:
    """Reservations and uncertain delivery consume the same marketing interval."""
    return (
        session.scalar(
            select(MessageLog.id)
            .where(
                MessageLog.user_id == user_id,
                MessageLog.status.in_(("reserved", "sent", "delivery_unknown")),
                or_(
                    MessageLog.status == "reserved",
                    MessageLog.sent_at.is_(None),
                    MessageLog.sent_at >= utcnow() - timedelta(hours=hours),
                ),
                or_(
                    MessageLog.purpose.startswith("pv_"),
                    MessageLog.purpose.startswith("bot45_"),
                    MessageLog.purpose.startswith("never_buyer_invite_"),
                ),
            )
            .limit(1)
        )
        is not None
    )


def scan(session: Session, settings: Settings, flags: FlagService) -> int:
    """Evaluate active rules and enqueue follow-up jobs (idempotent)."""
    if not flags.enabled("LIFECYCLE_AUTOMATION_ENABLED"):
        return 0
    rules = (
        session.execute(
            select(LifecycleRule).where(LifecycleRule.is_active == 1)  # noqa: E712
        )
        .scalars()
        .all()
    )
    scheduled = 0
    for rule in rules:
        cond = rule.conditions or {}
        cutoff = _activation_cutoff(cond)
        if cutoff is False:
            continue
        for user, gate_at in candidate_users(
            session,
            rule.trigger,
            limit=settings.lifecycle_scan_limit,
            not_before=cutoff,
        ):
            # optional condition filters
            cond = rule.conditions or {}
            if "marketing_cooldown_hours" in cond:
                hours = cond["marketing_cooldown_hours"]
                if type(hours) is not int or not 1 <= hours <= 168:
                    continue
                if marketing_cooldown(session, user.id, hours):
                    continue
            if cond.get("free_audience"):
                from pv_growth.free_config.audience import decision_for_user

                if not decision_for_user(session, settings, user.id).eligible:
                    continue
            if cond.get("segment") and user.segment != cond["segment"]:
                continue
            if cond.get("min_lead_score", 0) > user.lead_score:
                continue
            ordinal = messaging.next_send_ordinal(session, rule.code, user.id)
            if ordinal > rule.max_sends:
                continue
            template = messaging.get_template(session, rule.template_code)
            jobs.enqueue(
                session,
                "lifecycle.send_followup",
                {
                    "rule_code": rule.code,
                    "user_id": user.id,
                    "send_ordinal": ordinal,
                },
                scheduled_at=(gate_at or utcnow()) + timedelta(minutes=rule.delay_minutes),
                idempotency_key=(f"lc:{rule.code}:{user.id}:{ordinal}:v{template.version}"),
            )
            scheduled += 1
    if scheduled:
        log.info("lifecycle scan scheduled followups", scheduled=scheduled)
    return scheduled


def _stop_condition_met(session: Session, user_id: int, stop_events: list) -> bool:
    for event_type in stop_events or []:
        hit = session.execute(
            select(Event.id).where(Event.user_id == user_id, Event.event_type == event_type).limit(1)
        ).scalar_one_or_none()
        if hit is not None:
            return True
    return False


def send_followup(session: Session, settings: Settings, flags: FlagService, telegram, payload: dict) -> str:
    """Execute one follow-up with every guard rail. Returns a status string
    for observability: sent | duplicate | stopped | cooldown | max_sends | blocked."""
    if not flags.enabled("LIFECYCLE_AUTOMATION_ENABLED"):
        return "blocked"
    rule = session.execute(
        select(LifecycleRule)
        .where(LifecycleRule.code == payload["rule_code"])
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if rule is None or rule.is_active != 1:
        return "blocked"
    # Every marketing purpose serializes its fresh checks and durable reservation
    # on the same user row. The send engine commits that reservation before I/O.
    user = session.scalar(
        select(User)
        .where(User.id == payload["user_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or user.is_blocked:
        return "blocked"

    purpose = rule.code
    conditions = rule.conditions or {}
    cutoff = _activation_cutoff(conditions)
    if cutoff is False:
        return "blocked"
    if cutoff is not None:
        from pv_growth.lifecycle.triggers import _latest_event_at, gate_event_for, matches_trigger

        gate_at = _latest_event_at(session, user.id, gate_event_for(rule.trigger))
        if gate_at is None or gate_at < cutoff or not matches_trigger(session, user, rule.trigger):
            return "stopped"
    if "marketing_cooldown_hours" in conditions:
        hours = conditions["marketing_cooldown_hours"]
        if type(hours) is not int or not 1 <= hours <= 168:
            return "blocked"
        if marketing_cooldown(session, user.id, hours):
            return "cooldown"

    if (rule.conditions or {}).get("free_audience"):
        from pv_growth.free_config.audience import decision_for_user

        if not decision_for_user(session, settings, user.id).eligible:
            return "stopped"

    # STOP: purchase (or any listed event) kills the reminder
    if _stop_condition_met(session, user.id, rule.stop_conditions):
        log.info("followup stopped by condition", rule=purpose, user_id=user.id)
        return "stopped"

    # COOLDOWN: too soon after the last message of this purpose
    last = messaging.last_send_at(session, purpose, user.id)
    if last is not None and utcnow() - last < timedelta(hours=rule.cooldown_hours):
        return "cooldown"

    # MAX SENDS per purpose
    delivered = messaging.delivered_for_purpose(session, purpose, user.id)
    if len(delivered) >= rule.max_sends:
        return "max_sends"

    facts = payload.get("facts") or {}
    ordinal = int(payload.get("send_ordinal") or messaging.next_send_ordinal(session, purpose, user.id))
    if ordinal > rule.max_sends:
        return "max_sends"
    template = messaging.get_template(session, rule.template_code)
    row, created = messaging.send_user_message(
        session,
        telegram,
        user_id=user.id,
        template_code=rule.template_code,
        purpose=purpose,
        facts=facts,
        dedupe_key=f"lc:{purpose}:{user.id}:{ordinal}:v{template.version}",
        send_ordinal=ordinal,
    )
    if row is None:
        return "blocked"
    if not created:
        return "duplicate"
    return row.status


def cooldown_until(session, rule, user_id):
    hours = (rule.conditions or {}).get("marketing_cooldown_hours", 24)
    if type(hours) is not int or not 1 <= hours <= 168:
        return None
    logs = session.scalars(
        select(MessageLog).where(
            MessageLog.user_id == user_id,
            MessageLog.status.in_(("reserved", "sent", "delivery_unknown")),
            or_(
                MessageLog.purpose == rule.code,
                MessageLog.purpose.startswith("pv_"),
                MessageLog.purpose.startswith("bot45_"),
                MessageLog.purpose.startswith("never_buyer_invite_"),
            ),
        )
    ).all()
    if any(row.status == "reserved" or row.sent_at is None for row in logs):
        return None  # uncertain reservations require reconciliation, never blind retry
    ends = [
        row.sent_at + timedelta(hours=max(hours, rule.cooldown_hours) if row.purpose == rule.code else hours)
        for row in logs
    ]
    return max([utcnow() + timedelta(minutes=1), *ends]) + timedelta(seconds=1)


# ---- job handlers wired into the runner ----


@handler("lifecycle.scan")
def _scan_job(session: Session, settings: Settings, payload: dict) -> None:
    flags = FlagService(settings)
    scan(session, settings, flags)
    # keep segments fresh on every scan (cheap, recalculable)
    from pv_growth.segments.service import recompute_all

    recompute_all(session, batch_size=settings.segment_batch_size)


@handler("lifecycle.send_followup")
def _followup_job(session: Session, settings: Settings, payload: dict) -> None:
    flags = FlagService(settings)
    telegram = _telegram_or_fail(settings)
    try:
        status = send_followup(session, settings, flags, telegram, payload)
    except TelegramRetryableError as exc:
        raise RetryableJobError(str(exc)) from exc
    if status == "blocked":
        log.info("lifecycle followup blocked; treating as terminal no-op")
    if status == "cooldown":
        rule = session.scalar(select(LifecycleRule).where(LifecycleRule.code == payload["rule_code"]))
        until = cooldown_until(session, rule, payload["user_id"])
        if until is not None:
            raise DeferredJobError(until)


def _telegram_or_fail(settings: Settings):
    from pv_growth.telegram.client import get_telegram

    try:
        return get_telegram(settings)
    except Exception:  # noqa: BLE001 — no token in dev: use fake silently? NO:
        # record-and-fail beats silently-pretending; integration token is a blocker
        log.warning("telegram not configured; followup cannot send")
        raise
