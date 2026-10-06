import hashlib

import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import AppConfig
from pv_growth.free_config.plan_binding import TUNNEL_INBOUNDS, require_tunnel_plan


def configured(session, settings):
    s = settings.model_copy(
        update={
            "env": "production",
            "provisioning_base_url": "https://panel.test",
            "provisioning_sublink": "https://sub.test/link",
            "provisioning_inbound_ids": ",".join(map(str, TUNNEL_INBOUNDS)),
        }
    )
    binding = {
        "panel_id": 33,
        "active": True,
        "inbound_ids": list(TUNNEL_INBOUNDS),
        "endpoint_sha256": hashlib.sha256(s.provisioning_base_url.encode()).hexdigest(),
        "subscription_sha256": hashlib.sha256(s.provisioning_sublink.encode()).hexdigest(),
    }
    session.add(AppConfig(key="pv_free_growth_policy", value={"tunnel_plan": binding}))
    session.commit()
    return s


def test_main_tunnel_verified_binding_is_required(session, settings):
    s = configured(session, settings)
    assert require_tunnel_plan(session, s)["panel_id"] == 33


@pytest.mark.parametrize(
    "change",
    [
        {"provisioning_inbound_ids": "15,13,14"},
        {"provisioning_base_url": "https://wrong.test"},
        {"provisioning_sublink": "https://wrong.test/sub"},
    ],
)
def test_tunnel_drift_is_refused(session, settings, change):
    s = configured(session, settings)
    with pytest.raises(ValidationError):
        require_tunnel_plan(session, s.model_copy(update=change))


def test_missing_binding_fails_closed(session, settings):
    with pytest.raises(ValidationError):
        require_tunnel_plan(session, settings.model_copy(update={"env": "production"}))
