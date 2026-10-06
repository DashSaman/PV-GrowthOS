"""Message engine: strict template rendering + effect-logged sends.

Commercial facts (price, traffic, validity…) come ONLY from the structured
`facts` dict assembled by callers from campaign/provisioning data. Unknown
placeholders and unknown facts both fail loudly — a message can never ship
with invented numbers.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.core.logging import get_logger
from pv_growth.database.models import MessageLog, MessageTemplate, User
from pv_growth.database.types import utcnow
from pv_growth.events.service import ingest
from pv_growth.telegram.client import TelegramDeliveryUnknownError, TelegramRetryableError

log = get_logger("messaging")

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")

MESSAGE_INTENTS = (
    "acquisition",
    "education",
    "trial_followup",
    "purchase_recovery",
    "renewal",
    "expiry",
    "winback",
    "referral",
    "partner",
    "support",
)


def extract_placeholders(body: str) -> set[str]:
    return set(_PLACEHOLDER.findall(body))


def render(body: str, facts: dict) -> str:
    placeholders = extract_placeholders(body)
    missing = placeholders - set(facts)
    if missing:
        raise ValidationError(f"missing template facts: {sorted(missing)}")
    text = _PLACEHOLDER.sub(lambda m: str(facts[m.group(1)]), body)
    if "{{" in text or "}}" in text:
        raise ValidationError("malformed placeholder in body")
    return text


def get_template(session: Session, code: str) -> MessageTemplate:
    template = session.execute(
        select(MessageTemplate).where(
            MessageTemplate.code == code,
            MessageTemplate.is_active == 1,  # noqa: E712
        )
    ).scalar_one_or_none()
    if template is None:
        raise ValidationError(f"template not found: {code}")
    return template


def build_facts(session: Session, user_id: int, template: MessageTemplate, extra: dict | None = None) -> dict:
    """Base facts every template may use; extra facts come from the caller's
    structured data (campaign record / provisioning response)."""
    facts: dict = {"bot_link": "https://t.me/pvnetwork_bot"}
    user = session.get(User, user_id)
    if user is not None:
        facts["first_name"] = user.first_name or ""
    facts.update(extra or {})
    needed = extract_placeholders(template.body)
    missing = needed - set(facts)
    if missing:
        raise ValidationError(f"missing template facts: {sorted(missing)}")
    return {k: facts[k] for k in needed}


def record_send(
    session: Session,
    *,
    user_id: int,
    template_code: str,
    purpose: str,
    dedupe_key: str,
    status: str = "reserved",
    send_ordinal: int = 1,
    meta: dict | None = None,
) -> tuple[MessageLog | None, bool]:
    """Idempotent effect log: second call with same key is a no-op."""
    existing = session.execute(
        select(MessageLog).where(MessageLog.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, existing.status == "failed"
    row = MessageLog(
        user_id=user_id,
        template_code=template_code,
        purpose=purpose,
        dedupe_key=dedupe_key,
        status=status,
        meta=meta or {},
        send_ordinal=send_ordinal,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return session.execute(
            select(MessageLog).where(MessageLog.dedupe_key == dedupe_key)
        ).scalar_one(), False
    return row, True


def send_user_message(
    session: Session,
    telegram,
    *,
    user_id: int,
    template_code: str,
    purpose: str,
    facts: dict | None = None,
    cta_url: str | None = None,
    dedupe_key: str | None = None,
    send_ordinal: int = 1,
) -> tuple[MessageLog | None, bool]:
    """Reserve, send, and persist the actual delivery outcome idempotently."""
    template = get_template(session, template_code)
    rendered = render(template.body, build_facts(session, user_id, template, facts))
    key = dedupe_key or f"msg:{purpose}:{user_id}:{send_ordinal}:v{template.version}"

    user = session.get(User, user_id)
    if user is None or not user.telegram_chat_id:
        log.warning("cannot send: user has no chat", user_id=user_id)
        return None, False

    row, created = record_send(
        session,
        user_id=user_id,
        template_code=template_code,
        purpose=purpose,
        dedupe_key=key,
        status="reserved",
        send_ordinal=send_ordinal,
    )
    if not created:
        return row, False

    row.status = "reserved"
    row.attempts += 1
    row.meta = {}

    try:
        result = telegram.send_message(user.telegram_chat_id, rendered)
    except TelegramRetryableError as exc:
        row.status = "failed"
        row.meta = {"error": str(exc)[:200]}
        log.error("telegram send failed", user_id=user_id, purpose=purpose, error=str(exc))
        raise
    except TelegramDeliveryUnknownError as exc:
        row.status = "delivery_unknown"
        row.sent_at = utcnow()
        row.meta = {"error": str(exc)[:200]}
        log.warning(
            "telegram delivery outcome unknown",
            user_id=user_id,
            purpose=purpose,
            error=str(exc),
        )
        return row, True
    except Exception as exc:  # noqa: BLE001 — definite rejection/fake failure is evidence
        row.status = "failed"
        row.meta = {"error": str(exc)[:200]}
        log.error("telegram send failed", user_id=user_id, purpose=purpose, error=str(exc))
        return row, True

    row.status = "sent"
    row.sent_at = utcnow()
    row.meta = {"message_id": result.get("message_id")}
    ingest(
        session,
        "MESSAGE_SENT",
        user_id=user_id,
        idempotency_key=f"msgsent:{key}",
        metadata={"purpose": purpose, "template": template_code, "message_id": result.get("message_id")},
    )
    log.info("message sent", user_id=user_id, purpose=purpose)
    return row, True


def sends_for_purpose(session: Session, purpose: str, user_id: int) -> list[MessageLog]:
    return list(
        session.execute(
            select(MessageLog)
            .where(MessageLog.purpose == purpose, MessageLog.user_id == user_id)
            .order_by(MessageLog.send_ordinal, MessageLog.id)
        ).scalars()
    )


def delivered_for_purpose(session: Session, purpose: str, user_id: int) -> list[MessageLog]:
    return list(
        session.execute(
            select(MessageLog)
            .where(
                MessageLog.purpose == purpose,
                MessageLog.user_id == user_id,
                MessageLog.status.in_(("sent", "delivery_unknown")),
            )
            .order_by(MessageLog.send_ordinal, MessageLog.id)
        ).scalars()
    )


def next_send_ordinal(session: Session, purpose: str, user_id: int) -> int:
    rows = sends_for_purpose(session, purpose, user_id)
    failed = [row.send_ordinal for row in rows if row.status == "failed"]
    if failed:
        return min(failed)
    consumed = [row.send_ordinal for row in rows if row.status in {"sent", "delivery_unknown"}]
    return max(consumed, default=0) + 1


def last_send_at(session: Session, purpose: str, user_id: int) -> datetime | None:
    rows = delivered_for_purpose(session, purpose, user_id)
    return rows[-1].sent_at if rows else None
