"""Global live-client capacity, separate from the immutable daily byte budget."""

from sqlalchemy import or_, select

from pv_growth.database.models import FreeAllocation
from pv_growth.database.types import utcnow
from pv_growth.free_config.web_publication import lock_key
from pv_growth.provisioning.xui import client_email_for


def has_capacity(session, policy, adapter, *, current_key):
    if policy.get("server_autonomy_enabled") is not True:
        return True
    maximum = policy.get("owned_shared_active_limit", 2)
    if type(maximum) is not int or not 1 <= maximum <= 2:
        return False
    lock_key(session, "pv-shared-active-capacity")
    rows = list(
        session.scalars(
            select(FreeAllocation)
            .where(
                FreeAllocation.bucket == "public",
                FreeAllocation.id != current_key,
                FreeAllocation.status.not_in(["expired", "stale"]),
                or_(FreeAllocation.expires_at.is_(None), FreeAllocation.expires_at > utcnow()),
            )
            .order_by(FreeAllocation.created_at.desc())
            .limit(101)
        )
    )
    if len(rows) > 100:
        return False
    occupied = 0
    for row in rows:
        if not row.service_ref:
            ended = False
            if row.status == "pending_provision" and row.payload.get("provision_attempted") is False:
                continue  # this new-format preflight completed before any remote create
            if row.status not in {"reserved", "provision_reserved"}:
                try:
                    state = adapter.service_state(client_email_for(row.id))
                    ended = (
                        state.get("exists") is True
                        and state.get("traffic_limit_bytes") == 1024**3
                        and (
                            state.get("expired") is True
                            or state.get("quota_exhausted") is True
                            or state.get("enabled") is False
                        )
                    )
                except Exception:
                    ended = False
            occupied += not ended  # no mutation/replay of the unknown ledger
        else:
            try:
                if row.service_ref != client_email_for(row.id):
                    occupied += 1
                else:
                    state = adapter.service_state(row.service_ref)
                    ended = state.get("exists") is False or (
                        state.get("exists") is True
                        and (
                            state.get("expired") is True
                            or state.get("quota_exhausted") is True
                            or state.get("enabled") is False
                        )
                    )
                    occupied += not ended
            except Exception:
                occupied += 1  # unknown history never authorizes a new client
        if occupied >= maximum:
            return False
    return True
