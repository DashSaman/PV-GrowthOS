"""Catch unauthorized mailbox use, redirects, unbounded reads and send replay.

Only the external HTTP boundary is replaced; request validation and encoding
run through the real transport. No live account or message is used.
"""

import base64
import json
from email.message import EmailMessage

import httpx
import pytest

from pv_growth.acquisition.mail_transport import GmailTransport, MailUnavailable


def routes():
    return {
        **{f"a{i:02}": f"admin{i}@example.org" for i in range(1, 11)},
        **{f"s{i:02}": f"shop{i}@example.org" for i in range(1, 21)},
    }


def reply():
    mail = EmailMessage()
    mail["From"] = "PV Network <pvnetwork8@gmail.com>"
    mail["To"] = "admin1@example.org"
    mail["Subject"] = "Re: original proposal"
    mail["In-Reply-To"] = "<customer@example.org>"
    mail["References"] = "<original@gmail.com> <customer@example.org>"
    mail["Auto-Submitted"] = "auto-replied"
    mail.set_content("Reply only to a real inbound.")
    return mail


def transport(handler, mapping=None):
    return GmailTransport(
        client_id="fixture-client",
        client_secret="fixture-secret",
        refresh_token="fixture-refresh",
        recipients=mapping or routes(),
        http_transport=httpx.MockTransport(handler),
    )


def authorized(request):
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-access",
                "token_type": "Bearer",
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/gmail.send",
            },
        )
    if request.url.path.endswith("/profile"):
        return httpx.Response(200, json={"emailAddress": "pvnetwork8@gmail.com", "historyId": "42"})
    raise AssertionError("unexpected HTTP request")


def test_business_profile_is_verified_before_thread_reads():
    requests = []

    def server(request):
        requests.append(request)
        if request.url.path.endswith("/threads/abc123"):
            return httpx.Response(200, json={"id": "abc123", "messages": []})
        return authorized(request)

    with transport(server) as api:
        assert api.thread("abc123") == {"id": "abc123", "messages": []}
    assert [r.url.path for r in requests] == [
        "/token",
        "/gmail/v1/users/me/profile",
        "/gmail/v1/users/me/threads/abc123",
    ]
    assert requests[2].url.params["format"] == "full"
    assert requests[2].headers["authorization"] == "Bearer fixture-access"
    assert "fixture-secret" not in str(requests[0].url)
    assert b"grant_type=refresh_token" in requests[0].content


def test_wrong_mailbox_stops_before_read_or_send():
    paths = []

    def server(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "other@example.org"})
        return authorized(request)

    with transport(server) as api, pytest.raises(MailUnavailable, match="mailbox_mismatch"):
        api.thread("abc123")
    assert paths == ["/token", "/gmail/v1/users/me/profile"]


@pytest.mark.parametrize(
    "scope", ["", "https://www.googleapis.com/auth/gmail.send", "https://mail.google.com/"]
)
def test_missing_or_broader_grant_does_not_read_mail(scope):
    paths = []

    def server(request):
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-access",
                "scope": scope,
                "token_type": "Bearer",
                "expires_in": 3599,
            },
        )

    with transport(server) as api, pytest.raises(MailUnavailable, match="scope_mismatch"):
        api.thread("abc123")
    assert paths == ["/token"]


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_errors_are_never_retried_or_redirected_and_do_not_leak_secrets(status):
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(
            status, text="fixture-secret fixture-refresh", headers={"Location": "https://example.org/collect"}
        )

    with transport(server) as api, pytest.raises(MailUnavailable) as failure:
        api.thread("abc123")
    assert len(requests) == 1
    assert "fixture" not in str(failure.value)


def test_decoded_response_size_is_bounded():
    def server(request):
        return httpx.Response(200, content=b" " * (1048576 + 1))

    with transport(server) as api, pytest.raises(MailUnavailable, match="response_too_large"):
        api.thread("abc123")


def test_slow_stream_cannot_keep_one_request_alive_indefinitely(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("pv_growth.acquisition.mail_transport.time.monotonic", lambda: clock[0])

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 11.0
            yield b" " * 16384

    with transport(lambda request: httpx.Response(200, stream=SlowStream())) as api:
        with pytest.raises(MailUnavailable, match="response_time_bound"):
            api.thread("abc123")


@pytest.mark.parametrize("thread_id", ["../../profile", "https://example.org/", "abc?x=1", ""])
def test_thread_id_cannot_change_endpoint(thread_id):
    with transport(lambda request: pytest.fail("HTTP must not run")) as api:
        with pytest.raises(ValueError):
            api.thread(thread_id)


def test_original_search_is_one_bounded_sent_query_for_one_approved_route():
    requests = []

    def server(request):
        requests.append(request)
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"threads": [{"id": "abc123"}], "resultSizeEstimate": 1})
        return authorized(request)

    with transport(server) as api:
        assert api.find_sent("a01") == {"threads": [{"id": "abc123"}], "resultSizeEstimate": 1}
    query = requests[-1].url.params
    assert query["q"] == "in:sent to:admin1@example.org after:2026/10/18 before:2026/10/21"
    assert query["maxResults"] == "5"


def test_outside_route_or_incomplete_allowlist_cannot_reach_google():
    with transport(lambda request: pytest.fail("HTTP must not run")) as api:
        with pytest.raises(ValueError):
            api.find_sent("s21")
    mapping = routes()
    mapping.pop("a01")
    with pytest.raises(ValueError):
        transport(lambda request: pytest.fail("HTTP must not run"), mapping)


@pytest.mark.parametrize("change", ["to", "bcc", "cc", "from", "no_parent", "duplicate_to"])
def test_outbound_must_remain_single_recipient_reply(change):
    mail = reply()
    if change == "to":
        mail.replace_header("To", "other@example.org")
    elif change in {"bcc", "cc"}:
        mail[change] = "other@example.org"
    elif change == "from":
        mail.replace_header("From", "other@example.org")
    elif change == "no_parent":
        del mail["In-Reply-To"]
    else:
        # The parser can represent duplicate To headers even though the builder refuses them.
        mail._headers.append(("To", "other@example.org"))
    with transport(lambda request: pytest.fail("HTTP must not run")) as api:
        with pytest.raises(ValueError):
            api.send_reply("a01", "abc123", mail)


def test_send_uses_fixed_thread_and_one_mime_payload():
    sent = []

    def server(request):
        if request.url.path.endswith("/messages/send"):
            sent.append(request)
            return httpx.Response(200, json={"id": "def456", "threadId": "abc123", "labelIds": ["SENT"]})
        return authorized(request)

    with transport(server) as api:
        assert api.send_reply("a01", "abc123", reply()) == {"id": "def456", "threadId": "abc123"}
    assert len(sent) == 1
    payload = json.loads(sent[0].content)
    assert payload["threadId"] == "abc123"
    decoded = base64.urlsafe_b64decode(payload["raw"])
    assert b"To: admin1@example.org\r\n" in decoded
    assert b"In-Reply-To: <customer@example.org>\r\n" in decoded


def test_ambiguous_send_timeout_is_sanitized_and_not_repeated():
    sent = []

    def server(request):
        if request.url.path.endswith("/messages/send"):
            sent.append(request)
            raise httpx.ReadTimeout("secret transport detail", request=request)
        return authorized(request)

    with transport(server) as api, pytest.raises(MailUnavailable, match="http_transport_failed") as failure:
        api.send_reply("a01", "abc123", reply())
    assert len(sent) == 1
    assert "secret" not in str(failure.value)
