"""Reserved traffic always counts, including failures and unknown effects."""

import secrets
from datetime import UTC, date, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import AppConfig, FreeAllocation
from pv_growth.database.types import utcnow
from pv_growth.free_config.exclusive import _acquire_quota_locks

MIB = 1024**2
GIB = 1024**3
CAPS = {"public": 10 * GIB, "lottery": 50 * GIB}
GIFT_AUTHORIZED_DAY = date(2026, 10, 6)


def lock_day(session: Session, day: date) -> None:
    _acquire_quota_locks(session, 0, day)


def used_bytes(session: Session, day: date, bucket: str) -> int:
    session.flush()
    return int(
        session.scalar(
            select(func.coalesce(func.sum(FreeAllocation.traffic_bytes), 0)).where(
                FreeAllocation.day == day, FreeAllocation.bucket == bucket
            )
        )
        or 0
    )


def gift_day() -> date:
    now = utcnow()
    aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return aware.astimezone(timezone(timedelta(hours=3, minutes=30))).date()


def cap_for(session: Session, day: date, bucket: str) -> int:
    if bucket in CAPS:
        return CAPS[bucket]
    if bucket != "gift":
        raise ValidationError("invalid free reservation bucket")
    session.flush()
    row = session.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
    policy = row.value if row is not None and isinstance(row.value, dict) else {}
    cap = policy.get("gift_daily_bytes")
    if (
        policy.get("gift_enabled") is not True
        or type(cap) is not int
        or cap <= 0
        or cap > 1000 * GIB
        or day != GIFT_AUTHORIZED_DAY
        or policy.get("gift_day") != day.isoformat()
        or day != gift_day()
        or policy.get("gift_traffic_bytes") != 100 * MIB
        or type(policy.get("gift_traffic_bytes")) is not int
        or policy.get("gift_validity_hours") != 24
        or type(policy.get("gift_validity_hours")) is not int
    ):
        raise ValidationError("welcome gift budget unavailable")
    return cap


def reserve(
    session: Session,
    *,
    key: str,
    day: date,
    bucket: str,
    traffic_bytes: int,
    user_id: int | None = None,
    campaign_id: int | None = None,
) -> FreeAllocation:
    if (
        bucket not in {*CAPS, "gift"}
        or type(traffic_bytes) is not int
        or not 50 * MIB <= traffic_bytes <= GIB
        or not key
        or len(key) > 128
    ):
        raise ValidationError("invalid free reservation")
    lock_day(session, day)
    cap = cap_for(session, day, bucket)
    if bucket == "gift" and traffic_bytes != 100 * MIB:
        raise ValidationError("welcome gift requires exact 100 MiB")
    old = session.get(FreeAllocation, key)
    if old is not None:
        if (old.day, old.bucket, old.traffic_bytes, old.user_id, old.campaign_id) != (
            day,
            bucket,
            traffic_bytes,
            user_id,
            campaign_id,
        ):
            raise ValidationError("reservation replay mismatch")
        return old
    if used_bytes(session, day, bucket) + traffic_bytes > cap:
        raise ValidationError("daily traffic budget exhausted")
    row = FreeAllocation(
        id=key, day=day, bucket=bucket, traffic_bytes=traffic_bytes, user_id=user_id, campaign_id=campaign_id
    )
    session.add(row)
    session.flush()
    return row


def allocate_quotas(entrants: int, remaining: int) -> list[int]:
    if type(entrants) is not int or type(remaining) is not int or entrants < 0 or remaining < 0:
        raise ValidationError("invalid lottery allocation")
    count = min(entrants, 100, remaining // (50 * MIB))
    if remaining >= count * GIB:
        return [(50 + secrets.randbelow(975)) * MIB for _ in range(count)]
    remaining_units = min(remaining // MIB, count * 1024)
    quotas = []
    for index in range(count):
        left = count - index - 1
        low = max(50, remaining_units - left * 1024)
        high = min(1024, remaining_units - left * 50)
        quota = low + secrets.randbelow(high - low + 1)
        quotas.append(quota * MIB)
        remaining_units -= quota
    secrets.SystemRandom().shuffle(quotas)
    return quotas
