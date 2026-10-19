"""Real ledger + reply processing; only the remote Gmail boundary is a fixture."""

import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from pv_growth.acquisition.mail_replies import process, validate_config
from pv_growth.acquisition.mail_transport import MailUnavailable
from pv_growth.database.models import AppConfig

NOW = datetime(2026, 10, 19, 18, tzinfo=UTC)


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


def config():
    return {
        "campaign": "external-acquisition-20261019",
        "owner": "pvnetwork8@gmail.com",
        "authorized_until_utc": (NOW + timedelta(days=7)).isoformat(),
        "credentials": {
            "client_id": "fixture-client",
            "client_secret": "fixture-secret",
            "refresh_token": "fixture-refresh",
        },
        "recipients": [
            {
                "route": kind + f"{i:02}",
                "email": f"{kind}{i}@example.org",
                "name": "Business",
                "subject": "Proposal " + kind + str(i),
            }
            for kind, cap in [("a", 10), ("s", 20)]
            for i in range(1, cap + 1)
        ],
    }


def classification():
    return {
        "source": "owner_confirmed",
        "all_time_test_account_keys": [
            sha256(f"mirza-user:fixture-{i}".encode()).hexdigest() for i in range(3)
        ],
        "ordinary_test_share_estimate_percent": 50,
    }


def message(mid, *, body, sender="a1@example.org", sent=False, stamp=None):
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": "a1@example.org" if sent else "pvnetwork8@gmail.com"},
        {"name": "Subject", "value": "Proposal a1" if sent else "Re: Proposal a1"},
        {"name": "Message-ID", "value": "<" + mid + "@example.org>"},
    ]
    if not sent:
        headers.append(
            {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass header.from=example.org;"}
        )
    return {
        "id": mid,
        "threadId": "abc123",
        "labelIds": ["SENT"] if sent else ["INBOX"],
        "internalDate": str(int((stamp or NOW - timedelta(minutes=2)).timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


def thread(body="I am interested"):
    return {
        "id": "abc123",
        "messages": [
            message(
                "aaaa01",
                body="Original https://t.me/pvgrowthos_bot?start=social_ext261019_a01",
                sender="pvnetwork8@gmail.com",
                sent=True,
                stamp=NOW - timedelta(hours=1),
            ),
            message("bbbb01", body=body),
        ],
    }


class GmailFixture:
    def __init__(self, content=None):
        self.content = content or thread()
        self.sent = []
        self.searches = []
        self.reads = 0
        self.before_recheck = None
        self.timeout_send = False

    def find_sent(self, route):
        self.searches.append(route)
        return {"threads": [{"id": "abc123"}]} if route == "a01" else {"threads": []}

    def thread(self, thread_id):
        self.reads += 1
        if self.reads == 2 and self.before_recheck:
            self.before_recheck(self.content)
        return deepcopy(self.content)

    def send_reply(self, route, thread_id, mail):
        self.sent.append((route, thread_id, mail))
        if self.timeout_send:
            raise MailUnavailable("http_transport_failed")
        return {"id": "cccc01", "threadId": thread_id}


def run(settings, api, **updates):
    return process(
        settings,
        api,
        config=config(),
        catalogue=catalogue(),
        classification=classification(),
        now=NOW,
        **updates,
    )


def test_verified_human_reply_uses_brand_route_and_durable_claim(settings):
    api = GmailFixture()
    report = run(settings, api)
    assert report["routes"]["a01"] == "sent"
    assert len(api.sent) == 1 and len(api.searches) == 30
    route, tid, mail = api.sent[0]
    assert (route, tid, mail["To"]) == ("a01", "abc123", "a1@example.org")
    assert mail["In-Reply-To"] == "<bbbb01@example.org>"
    assert "social_ext261019_a01" in mail.get_body(preferencelist=("plain",)).get_content()
    assert "#e6b84b" in mail.get_body(preferencelist=("html",)).get_content()
    assert "fixture-secret" not in str(report) and "a1@example.org" not in str(report)
    assert "bbbb01" not in str(report)


def test_repeated_poll_and_same_intent_on_new_inbound_never_repeat(settings):
    api = GmailFixture()
    run(settings, api)
    assert run(settings, api)["routes"]["a01"] == "held_duplicate"
    api.content["messages"][-1] = message("bbbb02", body="still interested")
    assert run(settings, api)["routes"]["a01"] == "held_duplicate"
    assert len(api.sent) == 1


def test_ambiguous_send_remains_consumed_on_next_poll(settings):
    api = GmailFixture()
    api.timeout_send = True
    assert run(settings, api)["routes"]["a01"] == "unknown_send"
    assert run(settings, api)["routes"]["a01"] == "held_duplicate"
    assert len(api.sent) == 1


@pytest.mark.parametrize(
    "body,want",
    [
        ("No thanks", "stopped"),
        ("> I am interested", "hold_intent"),
        ("Maybe later", "hold_intent"),
        ("Can I have a discount?", "hold_terms"),
    ],
)
def test_silence_quotes_ambiguity_and_opt_out_do_not_trigger_mail(settings, body, want):
    api = GmailFixture(thread(body))
    assert run(settings, api)["routes"]["a01"] == want
    assert api.sent == []


def test_opt_out_survives_later_interested_text(settings):
    api = GmailFixture(thread("unsubscribe"))
    run(settings, api)
    api.content["messages"].append(message("bbbb02", body="interested", stamp=NOW - timedelta(minutes=1)))
    assert run(settings, api)["routes"]["a01"] == "stopped"
    assert api.sent == []


@pytest.mark.parametrize("break_original", ["recipient", "subject", "source", "sent_label", "from"])
def test_only_the_exact_original_sent_proposal_can_authorize_a_thread(settings, break_original):
    content = thread()
    original = content["messages"][0]
    if break_original == "source":
        original["payload"]["body"]["data"] = base64.urlsafe_b64encode(b"unrelated").decode()
    elif break_original == "sent_label":
        original["labelIds"] = ["INBOX"]
    else:
        key = {"recipient": "To", "subject": "Subject", "from": "From"}[break_original]
        next(h for h in original["payload"]["headers"] if h["name"] == key)["value"] = "wrong@example.org"
    api = GmailFixture(content)
    assert run(settings, api)["routes"]["a01"] == "held_original"
    assert api.sent == []


def test_delivery_report_is_not_an_interested_human(settings):
    content = thread()
    content["messages"][-1]["payload"]["headers"].append({"name": "Auto-Submitted", "value": "auto-replied"})
    api = GmailFixture(content)
    assert run(settings, api)["routes"]["a01"] == "ignore_automatic"
    assert api.sent == []


def test_similar_tracking_prefix_is_not_the_original_route(settings):
    content = thread()
    content["messages"][0]["payload"]["body"]["data"] = base64.urlsafe_b64encode(
        b"https://t.me/pvgrowthos_bot?start=social_ext261019_a010"
    ).decode()
    api = GmailFixture(content)
    assert run(settings, api)["routes"]["a01"] == "held_original"
    assert api.sent == []


def test_own_manual_reply_after_inbound_prevents_automatic_reply(settings):
    content = thread()
    content["messages"].append(
        message(
            "cccc01",
            body="Manual answer",
            sender="pvnetwork8@gmail.com",
            sent=True,
            stamp=NOW - timedelta(minutes=1),
        )
    )
    api = GmailFixture(content)
    assert run(settings, api)["routes"]["a01"] == "held_no_new_human_reply"
    assert api.sent == []


def test_opt_out_arriving_during_poll_is_rechecked_before_send(settings):
    api = GmailFixture()
    api.before_recheck = lambda content: content["messages"].append(
        message("bbbb02", body="unsubscribe", stamp=NOW - timedelta(minutes=1))
    )
    assert run(settings, api)["routes"]["a01"] == "stopped"
    api.before_recheck = None
    api.content = thread()
    assert run(settings, api)["routes"]["a01"] == "held_duplicate"
    assert api.sent == []


def test_failed_final_read_is_held_before_send_and_never_replayed(settings):
    api = GmailFixture()

    def unavailable(_):
        raise MailUnavailable("http_transport_failed")

    api.before_recheck = unavailable
    assert run(settings, api)["routes"]["a01"] == "held_before_send"
    api.before_recheck = None
    assert run(settings, api)["routes"]["a01"] == "held_duplicate"
    assert api.sent == []


def test_html_only_reply_is_held_instead_of_reading_quoted_sales_copy(settings):
    content = thread()
    content["messages"][-1]["payload"]["mimeType"] = "text/html"
    api = GmailFixture(content)
    assert run(settings, api)["routes"]["a01"] == "hold_intent"
    assert api.sent == []


@pytest.mark.parametrize("missing", ["catalogue", "classification"])
def test_missing_facts_stop_before_mailbox_access(settings, missing):
    api = GmailFixture()
    inputs = {"config": config(), "catalogue": catalogue(), "classification": classification(), "now": NOW}
    inputs[missing] = None
    with pytest.raises(ValueError):
        process(settings, api, **inputs)
    assert api.searches == [] and api.sent == []


@pytest.mark.parametrize("kind", ["extra", "missing", "duplicate", "owner", "expired", "extended"])
def test_private_configuration_cannot_expand_original_pilot(kind):
    value = config()
    if kind == "extra":
        value["recipients"].append({**value["recipients"][0], "route": "a11"})
    elif kind == "missing":
        value["recipients"].pop()
    elif kind == "duplicate":
        value["recipients"][-1] = value["recipients"][0]
    elif kind == "owner":
        value["owner"] = "other@example.org"
    elif kind == "expired":
        value["authorized_until_utc"] = NOW.isoformat()
    else:
        value["authorized_until_utc"] = (NOW + timedelta(days=32)).isoformat()
    with pytest.raises(ValueError):
        validate_config(value, NOW)


def test_private_credentials_never_enter_growth_report(settings, session):
    api = GmailFixture()
    report = run(settings, api)
    session.add(AppConfig(key="fixture-report", value=report))
    session.commit()
    text = str(session.get(AppConfig, "fixture-report").value)
    assert "fixture-refresh" not in text and "fixture-secret" not in text
    assert "recorded_service_toman" not in report  # existing sales report owns purchase classification


def test_production_cannot_replace_original_thirty_contacts_with_any_other_thirty(settings):
    api = GmailFixture()
    production = settings.model_copy(update={"env": "production"})
    with pytest.raises(ValueError, match="pilot recipient fingerprint"):
        run(production, api, lock_held=True)
    assert api.searches == [] and api.sent == []
