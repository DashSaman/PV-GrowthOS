import json
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

from pv_growth.acquisition import expansion_report as p
from pv_growth.acquisition import mail_replies
from pv_growth.database.models import AppConfig, Source
from tests.test_expansion_mail import config
from tests.test_needs_report import NOW, START, quality, snapshot

CODES = ["social:expand261024_e001", "social:expand261024_e002"]


def registry():
    routes = {r["route"]: r for r in config()["recipients"][:2]}
    fp = sha256(
        json.dumps(
            [[k, r["email"], r["subject"]] for k, r in sorted(routes.items())],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "stage": "registered",
        "campaign": "needs-expansion-20261024",
        "registered_at_utc": START.isoformat(),
        "owner_authorized_limit": 430,
        "manifest_fingerprint": fp,
        "sources": [
            {
                "key": c.rsplit("_", 1)[1],
                "start_parameter": c.replace("social:", "social_"),
                "source_code": c,
                "segment": "business",
            }
            for c in CODES
        ],
    }


def test_private_expansion_manifest_must_match_independent_registered_fingerprint(session, settings):
    reg = registry()
    session.add(AppConfig(key=p.CONFIG_KEY, value=reg))
    session.commit()
    value = config()
    value["recipients"] = value["recipients"][:2]
    routes = mail_replies.validate_config(value, NOW, campaign="needs-expansion-20261024")
    mail_replies.verify_pilot(routes, campaign="needs-expansion-20261024", settings=settings)
    routes["e001"]["subject"] = "Changed subject"
    with pytest.raises(ValueError):
        mail_replies.verify_pilot(routes, campaign="needs-expansion-20261024", settings=settings)


def test_native_report_uses_only_registered_batch_and_never_claims_new_customer(
    session, settings, monkeypatch
):
    reg = registry()
    reg.update(initial_send_closed=True, accepted_initial_count=2, last_accepted_at_utc=START.isoformat())
    session.add(AppConfig(key=p.CONFIG_KEY, value=reg))
    session.add(AppConfig(key="retail-sales:classification", value=quality()))
    for c in CODES:
        session.add(Source(code=c, kind="social", medium="business_email"))
    session.commit()
    now = START + timedelta(hours=73)
    monkeypatch.setattr(p, "utcnow", lambda: now)
    monkeypatch.setattr(
        p, "MirzaMySQLReader", lambda _: SimpleNamespace(fetch_retail_sales_snapshot=snapshot)
    )
    p.monitor(session, settings, {"hour": now.strftime("%Y%m%d%H")})
    r = session.get(AppConfig, p.PREFIX + ":latest").value
    assert r["data_complete"] and len(r["sources"]) == 2 and r["verified_new_customer_count"] is None
    assert (
        session.get(AppConfig, p.PREFIX + ":milestone:72").value["end_utc"]
        == (START + timedelta(hours=72)).isoformat()
    )
