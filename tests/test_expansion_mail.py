from datetime import timedelta

import pytest

from pv_growth.acquisition import expansion_mail_jobs, mail_replies, reply_ledger
from tests.test_needs_mail_replies import NOW, catalogue, classification
from tests.test_needs_mail_replies import config as oldconfig

C = "needs-expansion-20261024"


def config():
    c = oldconfig()
    c["campaign"] = C
    c["recipients"] = [
        {"route": f"e{i:03}", "email": f"e{i}@example.org", "name": "Business", "subject": f"Needs E{i}"}
        for i in range(1, 431)
    ]
    return c


class NoReply:
    def __init__(self):
        self.looked = []

    def find_sent(self, key):
        self.looked.append(key)
        return {"threads": []}


def run(settings, keys, campaign=C):
    a = NoReply()
    r = mail_replies.process(
        settings,
        a,
        config=config() if campaign == C else oldconfig(),
        catalogue=catalogue(),
        classification=classification(),
        now=NOW,
        campaign=campaign,
        route_keys=keys,
    )
    return a, r


def test_only_explicit_bounded_shard_is_polled(settings):
    keys = tuple(f"e{i:03}" for i in range(401, 431))
    a, r = run(settings, keys)
    assert a.looked == list(keys) and len(r["routes"]) == 30
    assert r["coverage"] == "selected_shard" and r["campaign_route_count"] == 430


@pytest.mark.parametrize("keys", [None, [], ["e001"] * 2, ["e999"], [f"e{i:03}" for i in range(1, 42)]])
def test_bad_or_unbounded_shard_never_opens_provider(settings, keys):
    with pytest.raises(ValueError):
        run(settings, keys)


def test_original_twenty_does_not_accept_subset_or_new_routes(settings):
    with pytest.raises(ValueError):
        run(settings, ["n01"], campaign="needs-led-acquisition-20261024")
    assert len(reply_ledger.routes_for("needs-led-acquisition-20261024")) == 20
    assert len(reply_ledger.routes_for("maps-acquisition-20261020")) == 10
    assert len(reply_ledger.routes_for("external-acquisition-20261019")) == 30


def test_all430_routes_are_visited_once_every_eleven_five_minute_slots():
    shards = [expansion_mail_jobs.shard(NOW + timedelta(minutes=5 * i)) for i in range(11)]
    assert set().union(*map(set, shards)) == reply_ledger.routes_for(C)
    assert sum(map(len, shards)) == 430 and max(map(len, shards)) <= 40
    assert expansion_mail_jobs.bucket(NOW) == "202610241205"


def test_expansion_claim_and_entry_url_are_separate_and_unknown_never_replays(settings):
    a = reply_ledger.claim_reply(settings, "e001", "abc001", "setup", campaign=C)
    b = reply_ledger.claim_reply(
        settings, "n01", "abc001", "setup", campaign="needs-led-acquisition-20261024"
    )
    assert a.message_key != b.message_key
    reply_ledger.finish_reply(settings, a, "unknown")
    assert reply_ledger.claim_reply(settings, "e001", "abc001", "setup", campaign=C) is None
    assert reply_ledger.source_url(C, "e001").endswith("social_expand261024_e001")


def test_deadline_resume_polls_skipped_tail_before_repeating_head(settings, monkeypatch):
    from pv_growth.acquisition import mail_replies

    clock = [0.0]
    monkeypatch.setattr(mail_replies.time, "monotonic", lambda: clock[0])

    class Slow(NoReply):
        def find_sent(self, key):
            clock[0] += 10
            return super().find_sent(key)

    selected = tuple(f"e{i:03}" for i in range(1, 41))
    a = Slow()
    first = mail_replies.process(
        settings,
        a,
        config=config(),
        catalogue=catalogue(),
        classification=classification(),
        now=NOW,
        campaign=C,
        route_keys=selected,
    )
    cursor = expansion_mail_jobs.resume_key(first, selected)
    assert cursor == "e013"
    second_order = selected[12:] + selected[:12]
    clock[0] = 0
    b = Slow()
    second = mail_replies.process(
        settings,
        b,
        config=config(),
        catalogue=catalogue(),
        classification=classification(),
        now=NOW,
        campaign=C,
        route_keys=second_order,
    )
    assert b.looked[0] == "e013" and not set(a.looked).intersection(b.looked)
    assert second["routes"]["e013"] == "held_original_lookup"
