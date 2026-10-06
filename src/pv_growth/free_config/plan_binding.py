"""Production grants must use the audited main-bot tunnel plan."""

import hashlib

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import AppConfig

TUNNEL_INBOUNDS = (17, 21, 37, 82, 83, 125, 126, 127, 128)


def require_tunnel_plan(session, settings) -> dict:
    if settings.env != "production":
        return {"panel_id": 33, "inbound_ids": list(TUNNEL_INBOUNDS)}
    row = session.get(AppConfig, "pv_free_growth_policy", populate_existing=True)
    binding = (
        (row.value or {}).get("tunnel_plan") if row is not None and isinstance(row.value, dict) else None
    )
    try:
        configured = tuple(int(x.strip()) for x in settings.provisioning_inbound_ids.split(","))
    except (ValueError, AttributeError):
        raise ValidationError("tunnel plan configuration unavailable") from None

    def fingerprint(value):
        return hashlib.sha256(value.rstrip("/").encode()).hexdigest()

    if (
        not isinstance(binding, dict)
        or binding.get("panel_id") != 33
        or binding.get("active") is not True
        or binding.get("inbound_ids") != list(TUNNEL_INBOUNDS)
        or configured != TUNNEL_INBOUNDS
        or binding.get("endpoint_sha256") != fingerprint(settings.provisioning_base_url)
        or binding.get("subscription_sha256") != fingerprint(settings.provisioning_sublink)
    ):
        raise ValidationError("only the verified main-bot tunnel plan may grant free service")
    return binding
