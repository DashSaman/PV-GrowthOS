"""Fail-closed eligibility for private free offers; Mirza remains read-only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.errors import ValidationError
from pv_growth.database.models import AppConfig, User
from pv_growth.database.types import utcnow
from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader

POLICY_KEY = "free_audience_policy"
DEFAULT_DORMANT_DAYS = 45
_TEHRAN = timezone(timedelta(hours=3, minutes=30))
_POTENTIALLY_ACTIVE = {"active", "end_of_time", "end_of_volume", "sendedwarn", "send_on_hold"}
_TERMINAL = {"disabledn", "removetime", "removevolume", "removebyuser"}
_NOT_PAID = {"unpaid", "unsuccessful"}
_OTHER_PAID_TYPES = {
    "extend_user",
    "extra_user",
    "extra_not_user",
    "extends_not_user",
    "extend_user_by_pvnetwork_gateway",
    "renew_verified_by_pvnetwork_gateway",
    "extra_volume_verified_by_pvnetwork_gateway",
}


@dataclass(frozen=True)
class AudienceDecision:
    eligible: bool
    reason: str


def _amount(value) -> Decimal:
    result = Decimal(str(value).replace(",", "").strip())
    if not result.is_finite() or result < 0:
        raise ValueError("invalid amount")
    return result


def _time(value) -> datetime:
    text = str(value or "").strip()
    if text.isdigit() and len(text) == 10:
        return datetime.fromtimestamp(int(text), UTC)
    # Mirza's service_other and Payment_report use Tehran wall-clock dates.
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_TEHRAN).astimezone(UTC)
        except ValueError:
            continue
    raise ValueError("invalid business timestamp")


def evaluate_history(orders, other, payments, *, now: datetime, dormant_days: int) -> AudienceDecision:
    """Complete SELECT results are required; no ingestion-time purchase guesses.

    An active invoice is conservatively excluded even when its original sale
    predates the cutoff: renewals and manual extensions can keep it alive.
    """
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    cutoff = now - timedelta(days=dormant_days)
    dates = []
    try:
        for order in orders:
            status = str(order.get("Status") or "").lower()
            if status in _POTENTIALLY_ACTIVE:
                return AudienceDecision(False, "active_or_unverified_service")
            if status in _NOT_PAID:
                continue
            if status not in _TERMINAL:
                return AudienceDecision(False, "unknown_invoice_status")
            if _amount(order.get("price_product")) > 0:
                dates.append(_time(order.get("time_sell")))
        for row in other:
            kind, status = str(row.get("type") or ""), str(row.get("status") or "").lower()
            if kind in {"gift_time", "gift_volume", "transfertouser"}:
                continue
            if kind not in _OTHER_PAID_TYPES:
                return AudienceDecision(False, "unknown_service_operation")
            if status == "unpaid":
                continue
            if status not in {"", "paid"}:
                return AudienceDecision(False, "unknown_service_payment_status")
            if _amount(row.get("price")) > 0:
                dates.append(_time(row.get("time")))
        for row in payments:
            status = str(row.get("payment_Status") or "").lower()
            if status in {"expire", "waiting", "reject"}:
                continue
            if status != "paid":
                return AudienceDecision(False, "unknown_payment_status")
            if _amount(row.get("price")) > 0:
                dates.append(_time(row.get("time")))
                if row.get("at_updated"):
                    dates.append(_time(row["at_updated"]))
    except (ValueError, InvalidOperation, OverflowError, OSError, TypeError):
        return AudienceDecision(False, "invalid_history")
    if any(stamp > now for stamp in dates):
        return AudienceDecision(False, "invalid_history")
    if dates and max(dates) > cutoff:
        return AudienceDecision(False, "recent_purchase")
    return AudienceDecision(True, "dormant" if dates else "never_purchased")


def decision_for_user(session: Session, settings: Settings, user_id: int) -> AudienceDecision:
    policy = session.get(AppConfig, POLICY_KEY)
    config = policy.value if policy else {}
    if not isinstance(config, dict):
        return AudienceDecision(False, "invalid_policy")
    enabled = config.get("enabled", settings.env == "production")
    if not isinstance(enabled, bool):
        return AudienceDecision(False, "invalid_policy")
    if not enabled:
        return AudienceDecision(True, "policy_disabled")
    days = config.get("dormant_days", DEFAULT_DORMANT_DAYS)
    if type(days) is not int or not 1 <= days <= 365:
        return AudienceDecision(False, "invalid_policy")
    user = session.get(User, user_id)
    if user is None or user.is_blocked or not user.telegram_user_id:
        return AudienceDecision(False, "missing_identity")
    try:
        history = MirzaMySQLReader(settings).fetch_free_audience_history(user.telegram_user_id)
    except Exception:  # noqa: BLE001 — unknown eligibility must never become a free grant
        return AudienceDecision(False, "history_unavailable")
    return evaluate_history(*history, now=utcnow(), dormant_days=days)


def require_free_audience(session: Session, settings: Settings, user_id: int) -> None:
    if not decision_for_user(session, settings, user_id).eligible:
        raise ValidationError(
            "این پیشنهاد برای افراد بدون خرید اخیر و بدون سرویس فعال است؛ وضعیت شما تأیید نشد."
        )


def guard_reserved_claim(session: Session, settings: Settings, claim) -> bool:
    decision = decision_for_user(session, settings, claim.user_id)
    if decision.eligible:
        return True
    claim.status = "audience_blocked"
    claim.last_provision_error = decision.reason
    return False
