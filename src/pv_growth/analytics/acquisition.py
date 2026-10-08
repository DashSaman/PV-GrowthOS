"""Aggregate observations, explicitly separated from incremental revenue."""

from datetime import UTC, timedelta

from sqlalchemy import func, select

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import AttributionTouch, Event, Source


def _utc_naive(value):
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def source_observations(session, source_code, *, start, end):
    start, end = _utc_naive(start), _utc_naive(end)
    if not start < end or end - start > timedelta(days=31):
        raise ValidationError("invalid fixed reporting window")
    source = session.scalar(select(Source).where(Source.code == source_code))
    result = {
        "source": source_code,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "distinct_first_touches": 0,
        "observed_payers": 0,
        "observed_payments": 0,
        "observed_amount_toman": 0,
        "status_transition_amount_toman": 0,
        "first_observed_or_unclassified_payments": 0,
        "invalid_amount_observations": 0,
        "incremental_revenue_proven": False,
        "payment_time_basis": "Mirza ingestion/observation time; not bank settlement time",
    }
    if source is None:
        return result
    touch_filter = (
        AttributionTouch.source_id == source.id,
        AttributionTouch.kind == "first",
        AttributionTouch.occurred_at >= start,
        AttributionTouch.occurred_at < end,
    )
    result["distinct_first_touches"] = session.scalar(
        select(func.count(func.distinct(AttributionTouch.user_id))).where(*touch_filter)
    )
    events = list(
        session.scalars(
            select(Event)
            .join(AttributionTouch, AttributionTouch.user_id == Event.user_id)
            .where(
                *touch_filter,
                Event.event_type == "PAYMENT_SUCCESS",
                Event.occurred_at >= AttributionTouch.occurred_at,
                Event.occurred_at >= start,
                Event.occurred_at < end,
            )
            .order_by(Event.occurred_at, Event.id)
            .limit(10001)
        )
    )
    if len(events) > 10000:
        raise ValidationError("report exceeds bounded event limit")
    seen, payers = set(), set()
    for event in events:
        meta = event.metadata_json or {}
        if meta.get("source") != "mirza_db":
            continue
        key = (event.user_id, meta.get("mirza_invoice_id") or event.id)
        if key in seen:
            continue
        seen.add(key)
        amount = meta.get("amount")
        if type(amount) is not int or not 0 < amount <= 10**12:
            result["invalid_amount_observations"] += 1
            continue
        payers.add(event.user_id)
        result["observed_payments"] += 1
        result["observed_amount_toman"] += amount
        if meta.get("observation_kind") == "status_transition":
            result["status_transition_amount_toman"] += amount
        else:
            result["first_observed_or_unclassified_payments"] += 1
    result["observed_payers"] = len(payers)
    return result
