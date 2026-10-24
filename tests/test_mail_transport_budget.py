import httpx
import pytest

from pv_growth.acquisition import mail_budget
from pv_growth.acquisition.mail_transport import GmailTransport, MailUnavailable
from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig
from pv_growth.database.types import utcnow
from tests.test_mail_transport import authorized, reply, routes


def make(settings, server):
    return GmailTransport(
        client_id="fixture-client",
        client_secret="fixture-secret",
        refresh_token="fixture-refresh",
        recipients=routes(),
        http_transport=httpx.MockTransport(server),
        budget_settings=settings,
    )


def baseline(settings):
    now = utcnow()
    mail_budget.initialize(settings, observed_sent=[], observed_at=now, initial_already_attempted=20, now=now)


def test_transport_paces_profile_snapshot_and_actual_post(settings, monkeypatch):
    from pv_growth.acquisition import mail_api_quota

    baseline(settings)
    costs = []
    monkeypatch.setattr(mail_api_quota, "wait", lambda s, cost: costs.append(cost))

    def server(req):
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": []})
        if req.url.path.endswith("/messages/send"):
            return httpx.Response(200, json={"id": "abcdef", "threadId": "abc123"})
        return authorized(req)

    with make(settings, server) as api:
        api.send_reply("a01", "abc123", reply())
    assert costs == [1, 5, 100]


def test_unavailable_api_rate_storage_never_reaches_provider(settings, monkeypatch):
    from pv_growth.acquisition import mail_api_quota

    def fail(*_):
        raise ValueError("fixture unavailable rate storage")

    monkeypatch.setattr(mail_api_quota, "wait", fail)
    calls = []

    def server(req):
        calls.append(req.url.path)
        return authorized(req)

    with make(settings, server) as api, pytest.raises(MailUnavailable, match="api_rate_budget_unavailable"):
        api.find_sent("a01")
    assert calls == ["/token"]


def test_committed_shared_budget_exists_before_send_post(settings):
    baseline(settings)
    posts = []

    def server(req):
        if req.url.path.endswith("/messages"):
            assert req.url.params["maxResults"] == "500" and "after:" in req.url.params["q"]
            return httpx.Response(200, json={"messages": []})
        if req.url.path.endswith("/messages/send"):
            with session_scope(settings) as db:
                assert db.get(AppConfig, mail_budget.KEY).value["attempts"] == 1
            posts.append(req)
            return httpx.Response(200, json={"id": "abcdef", "threadId": "abc123"})
        return authorized(req)

    with make(settings, server) as api:
        api.send_reply("a01", "abc123", reply())
    assert len(posts) == 1
    with session_scope(settings) as db:
        assert db.get(AppConfig, mail_budget.KEY).value["events"][0]["receipt_hash"]


@pytest.mark.parametrize(
    "snapshot",
    [{"messages": []}, {"messages": [{"id": "1"}], "nextPageToken": "more"}, {"messages": [{"id": "bad?"}]}],
)
def test_missing_budget_or_incomplete_snapshot_cannot_send(settings, snapshot):
    def server(req):
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json=snapshot)
        if req.url.path.endswith("/messages/send"):
            pytest.fail("POST reached before budget")
        return authorized(req)

    with make(settings, server) as api, pytest.raises(MailUnavailable):
        api.send_reply("a01", "abc123", reply())


def test_ambiguous_provider_post_keeps_budget_and_cannot_repeat(settings):
    baseline(settings)
    posts = []

    def server(req):
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": []})
        if req.url.path.endswith("/messages/send"):
            posts.append(req)
            raise httpx.ReadTimeout("fixture")
        return authorized(req)

    with make(settings, server) as api:
        for _ in range(2):
            with pytest.raises(MailUnavailable):
                api.send_reply("a01", "abc123", reply())
    assert len(posts) == 1
    with session_scope(settings) as db:
        v = db.get(AppConfig, mail_budget.KEY).value
        assert v["attempts"] == 1 and v["events"][0]["receipt_hash"] is None
