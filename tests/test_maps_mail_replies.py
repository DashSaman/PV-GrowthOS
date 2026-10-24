"""Independent ten-contact autonomy; only provider IO is replaced in tests."""

import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from pv_growth.acquisition import mail_replies, mail_transport, reply_ledger
from pv_growth.acquisition.reply_message import build_reply
from pv_growth.database.models import AppConfig, Job

CAMPAIGN = "maps-acquisition-20261020"
NOW = datetime(2026, 10, 20, 12, 5, tzinfo=UTC)


def config():
    return {
        "campaign": CAMPAIGN,
        "owner": "pvnetwork8@gmail.com",
        "authorized_until_utc": (NOW + timedelta(days=7)).isoformat(),
        "credentials": {"client_id": "fixture", "client_secret": "fixture", "refresh_token": "fixture"},
        "recipients": [
            {
                "route": f"m{i:02}",
                "email": f"m{i}@example.org",
                "name": "Business",
                "subject": f"Proposal M{i}",
            }
            for i in range(1, 11)
        ],
    }


def catalogue():
    return {
        "source": "mirza_local_readonly",
        "observed_at_utc": NOW.isoformat(),
        "plans": [
            {
                "code": "fixture",
                "name": "fixture",
                "location": "fixture",
                "volume_gb": 20,
                "duration_days": 31,
                "price_toman": 100000,
            }
        ],
        "payment": {
            "direct_buy": False,
            "minimum_wallet_toman": 100000,
            "card_to_support": True,
            "support_handle": "PVNetwork1",
        },
    }


def classification():
    return {
        "source": "owner_confirmed",
        "all_time_test_account_keys": [
            sha256(f"mirza-user:fixture-{i}".encode()).hexdigest() for i in range(3)
        ],
        "ordinary_test_share_estimate_percent": 50,
    }


def message(i, *, inbound=False, text=None):
    tid = f"abc{i:03}"
    return {
        "id": f"{'bb' if inbound else 'aa'}{i:04}",
        "threadId": tid,
        "labelIds": ["INBOX"] if inbound else ["SENT"],
        "internalDate": str(int((NOW - timedelta(minutes=1 if inbound else 30)).timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": f"m{i}@example.org" if inbound else "pvnetwork8@gmail.com"},
                {"name": "To", "value": "pvnetwork8@gmail.com" if inbound else f"m{i}@example.org"},
                {"name": "Subject", "value": ("Re: " if inbound else "") + f"Proposal M{i}"},
                {"name": "Message-ID", "value": f"<{'human' if inbound else 'original'}{i}@example.org>"},
                *(
                    [
                        {
                            "name": "Authentication-Results",
                            "value": "mx.google.com; dmarc=pass header.from=example.org;",
                        }
                    ]
                    if inbound
                    else []
                ),
            ],
            "body": {
                "data": base64.urlsafe_b64encode(
                    (text or f"Original https://t.me/pvgrowthos_bot?start=social_maps261020_m{i:02}").encode()
                ).decode()
            },
        },
    }


class Gmail:
    def __init__(self, response=None):
        self.threads = {f"abc{i:03}": {"id": f"abc{i:03}", "messages": [message(i)]} for i in range(1, 11)}
        if response is not None:
            self.threads["abc001"]["messages"].append(message(1, inbound=True, text=response))
        self.sent = []
        self.timeout = False

    def find_sent(self, route):
        return {"threads": [{"id": f"abc{int(route[1:]):03}"}]}

    def thread(self, tid):
        return deepcopy(self.threads[tid])

    def send_reply(self, route, tid, mail):
        self.sent.append((route, tid, mail))
        if self.timeout:
            raise mail_transport.MailUnavailable("http_transport_failed")
        return {"id": "cc0011", "threadId": tid}


def run(settings, api, **updates):
    return mail_replies.process(
        settings,
        api,
        config=config(),
        catalogue=catalogue(),
        classification=classification(),
        now=NOW,
        campaign=CAMPAIGN,
        **updates,
    )


def test_maps_claims_have_independent_namespace_and_ambiguous_send_never_replays(settings):
    original = reply_ledger.claim_reply(settings, "a01", "abc01", "interest")
    maps = reply_ledger.claim_reply(settings, "m01", "abc01", "interest", campaign=CAMPAIGN)
    assert original.message_key != maps.message_key
    reply_ledger.finish_reply(settings, maps, "unknown")
    assert reply_ledger.claim_reply(settings, "m01", "abc01", "interest", campaign=CAMPAIGN) is None
    assert reply_ledger.claim_reply(settings, "m01", "abc02", "interest", campaign=CAMPAIGN) is None


def test_maps_config_is_exact_ten_and_cannot_be_consumed_as_original_thirty():
    assert len(mail_replies.validate_config(config(), NOW, campaign=CAMPAIGN)) == 10
    with pytest.raises(ValueError):
        mail_replies.validate_config(config(), NOW)
    changed = config()
    changed["recipients"].append(dict(changed["recipients"][0], route="a01"))
    with pytest.raises(ValueError):
        mail_replies.validate_config(changed, NOW, campaign=CAMPAIGN)


def test_maps_reply_preserves_black_gold_original_inline_logo_and_same_source():
    mail = build_reply(
        route="m03",
        recipient="m3@example.org",
        name="Business",
        subject="Proposal M3",
        intent="price",
        inbound_id="bb0003",
        in_reply_to="<human3@example.org>",
        references=[],
        catalogue=catalogue(),
        now=NOW,
        campaign=CAMPAIGN,
    )
    body = mail.get_body(preferencelist=("html",)).get_content()
    assert "social_maps261020_m03" in body and "social_ext261019_" not in body
    assert "#080604" in body and "#e6b84b" in body and "{{" not in body
    images = [p for p in mail.walk() if p.get_content_type() == "image/png"]
    assert (
        len(images) == 1
        and sha256(images[0].get_payload(decode=True)).hexdigest()
        == "189fa0ce9f9496c321f2ec411cbe1eabbce45438d5dfc3931917c93dae225ae3"
    )


def test_ten_silent_threads_never_send_and_keep_separate_sales_report(settings):
    api = Gmail()
    result = run(settings, api)
    assert result["data_complete"] and len(result["routes"]) == 10 and api.sent == []
    assert set(result["routes"].values()) == {"held_no_new_human_reply"}
    assert result["sales_report_key"] == "retail-maps:20261020:latest"
    assert result["confirmed_internal_test_account_count"] == 3 and result["reminders_sent"] == 0


def test_interested_human_gets_one_branded_reply_and_cannot_repeat(settings):
    api = Gmail("I am interested")
    assert run(settings, api)["routes"]["m01"] == "sent"
    assert run(settings, api)["routes"]["m01"] == "held_duplicate"
    assert (
        len(api.sent) == 1
        and "social_maps261020_m01" in api.sent[0][2].get_body(preferencelist=("plain",)).get_content()
    )


def test_maps_unknown_send_is_durable_and_not_retried(settings):
    api = Gmail("I am interested")
    api.timeout = True
    assert run(settings, api)["routes"]["m01"] == "unknown_send"
    assert run(settings, api)["routes"]["m01"] == "held_duplicate"
    assert len(api.sent) == 1


@pytest.mark.parametrize("change", ["dsn", "spam", "unverified", "optout", "quote"])
def test_non_genuine_or_optout_does_not_trigger_maps_reply(settings, change):
    api = Gmail("I am interested")
    msg = api.threads["abc001"]["messages"][-1]
    if change == "dsn":
        msg["payload"]["headers"].append({"name": "Auto-Submitted", "value": "auto-generated"})
    elif change == "spam":
        msg["labelIds"] = ["SPAM"]
    elif change == "unverified":
        msg["payload"]["headers"] = [
            h for h in msg["payload"]["headers"] if h["name"] != "Authentication-Results"
        ]
    else:
        text = "unsubscribe" if change == "optout" else "> I am interested"
        msg["payload"]["body"]["data"] = base64.urlsafe_b64encode(text.encode()).decode()
    result = run(settings, api)
    assert result["routes"]["m01"] != "sent" and api.sent == []
    if change == "optout":
        api.threads["abc001"]["messages"][-1] = message(1, inbound=True, text="I am interested")
        assert run(settings, api)["routes"]["m01"] == "held_duplicate"


def test_maps_transport_is_fixed_ten_and_cannot_widen_original_allowlist():
    cls = getattr(mail_transport, "MapsGmailTransport", None)
    assert cls is not None, "separate bounded Maps transport is not implemented"
    mapping = {r["route"]: r["email"] for r in config()["recipients"]}
    with cls(**config()["credentials"], recipients=mapping):
        pass
    with pytest.raises(ValueError):
        mail_transport.GmailTransport(**config()["credentials"], recipients=mapping)


def test_maps_native_job_is_distinct_quarter_hour_once_max_one(session, tmp_path, monkeypatch):
    from pv_growth.acquisition import maps_mail_jobs

    path = tmp_path / "maps.json"
    path.write_text("{}")
    monkeypatch.setattr(maps_mail_jobs, "CONFIG_PATH", path)
    maps_mail_jobs.enqueue(session, NOW)
    maps_mail_jobs.enqueue(session, NOW)
    jobs = list(session.scalars(select(Job).where(Job.job_type == "retail.maps_mail_replies")))
    assert len(jobs) == 1 and jobs[0].max_attempts == 1 and jobs[0].payload == {"bucket": "202610201200"}


def test_maps_production_recipient_fingerprint_rejects_other_ten():
    with pytest.raises(ValueError):
        mail_replies.verify_pilot({r["route"]: r for r in config()["recipients"]}, campaign=CAMPAIGN)


def install_facts(session):
    session.add(
        AppConfig(
            key="retail-maps:campaign:20261020",
            value={
                "stage": "registered",
                "campaign": CAMPAIGN,
                "owner_authorized_limit": 10,
                "registered_at_utc": (NOW - timedelta(hours=1)).isoformat(),
                "sources": [
                    {
                        "key": f"m{i:02}",
                        "source_code": f"social:maps261020_m{i:02}",
                        "start_parameter": f"social_maps261020_m{i:02}",
                        "segment": "business",
                    }
                    for i in range(1, 11)
                ],
            },
        )
    )
    session.add(AppConfig(key="retail-sales:classification", value=classification()))
    session.add(AppConfig(key="retail-catalogue:active", value=catalogue()))
    session.commit()


def test_maps_native_monitor_requires_registry_facts_and_runs_once(session, settings, monkeypatch):
    from contextlib import contextmanager

    from pv_growth.acquisition import maps_mail_jobs

    install_facts(session)
    api = Gmail("I am interested")

    @contextmanager
    def provider(**_):
        yield api

    monkeypatch.setattr(maps_mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(maps_mail_jobs, "load_config", lambda *_: config())
    monkeypatch.setattr(maps_mail_jobs, "GmailTransport", provider)
    maps_mail_jobs.monitor(session, settings, {"bucket": "202610201200"})
    report = session.get(AppConfig, "maps-mail-replies:20261020:latest").value
    assert report["status"] == "ok" and report["data_complete"] and len(report["routes"]) == 10
    assert report["routes"]["m01"] == "sent" and report["sales_report_key"] == "retail-maps:20261020:latest"
    assert report["reminders_sent"] == 0 and report["confirmed_internal_test_account_count"] == 3
    maps_mail_jobs.monitor(session, settings, {"bucket": "202610201200"})
    assert len(api.sent) == 1


def test_maps_native_monitor_stale_payload_never_opens_auth(session, settings, monkeypatch):
    from pv_growth.acquisition import maps_mail_jobs

    monkeypatch.setattr(maps_mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(maps_mail_jobs, "load_config", lambda *_: pytest.fail("stale job opened auth"))
    maps_mail_jobs.monitor(session, settings, {"bucket": "202610201145"})
    assert session.get(AppConfig, "maps-mail-replies:20261020:latest") is None


def test_maps_native_advisory_lock_contention_does_not_read_or_send(session, settings, monkeypatch):
    from pv_growth.acquisition import maps_mail_jobs

    monkeypatch.setattr(maps_mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(maps_mail_jobs, "load_config", lambda *_: pytest.fail("contended job opened auth"))
    with maps_mail_jobs.campaign_lock(settings) as acquired:
        assert acquired
        maps_mail_jobs.monitor(session, settings, {"bucket": "202610201200"})
    assert session.get(AppConfig, "maps-mail-replies:20261020:latest") is None


def test_maps_missing_classification_stops_before_provider(session, settings, monkeypatch):
    from pv_growth.acquisition import maps_mail_jobs

    install_facts(session)
    session.delete(session.get(AppConfig, "retail-sales:classification"))
    session.commit()
    monkeypatch.setattr(maps_mail_jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(maps_mail_jobs, "load_config", lambda *_: config())
    monkeypatch.setattr(
        maps_mail_jobs, "GmailTransport", lambda **_: pytest.fail("classification missing but auth opened")
    )
    maps_mail_jobs.monitor(session, settings, {"bucket": "202610201200"})
    value = session.get(AppConfig, "maps-mail-replies:20261020:latest").value
    assert value["status"] == "unavailable" and value["data_complete"] is False
