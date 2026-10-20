"""Fixed-endpoint Gmail transport for the original thirty-contact pilot.

No redirects, retries, mailbox mutation or logging of provider responses. The
processor must verify the original thread and commit its durable send claim
before invoking send_reply; a send exception always consumes that claim.
"""

import base64
import json
import re
import time
from email import policy
from email.utils import getaddresses

import httpx

from pv_growth.acquisition.reply_ledger import ROUTES

OWNER = "pvnetwork8@gmail.com"
SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    }
)
ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"


class MailUnavailable(Exception):
    """Fixed diagnostic codes only; never wrap provider text or credentials."""


def _thread_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Fa-f0-9]{1,128}", value):
        raise ValueError("invalid Gmail thread identifier")
    return value


def _addresses(mail, header):
    values = mail.get_all(header, [])
    if len(values) != 1:
        return []
    return [address.lower() for _, address in getaddresses(values)]


class GmailTransport:
    def __init__(self, *, client_id, client_secret, refresh_token, recipients, http_transport=None):
        if (
            not isinstance(recipients, dict)
            or set(recipients) != ROUTES
            or len(set(recipients.values())) != 30
            or any(
                not isinstance(v, str)
                or not re.fullmatch(
                    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9][A-Za-z0-9.-]+",
                    v,
                )
                or v.lower() == OWNER
                for v in recipients.values()
            )
            or any(
                not isinstance(v, str)
                or not v
                or len(v) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in v)
                for v in (client_id, client_secret, refresh_token)
            )
        ):
            raise ValueError("invalid private pilot configuration")
        self._recipients = dict(recipients)
        self._credentials = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        self._http = httpx.Client(
            transport=http_transport,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        )
        self._attempted = False
        self._ready = False
        self._token = ""
        self._expires = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()
        self._token = ""
        self._credentials.clear()

    def _json(self, method, url, **kwargs):
        deadline = time.monotonic() + 10
        try:
            with self._http.stream(method, url, **kwargs) as response:
                if response.status_code != 200:
                    raise MailUnavailable("http_status_" + str(response.status_code))
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=16384):
                    if time.monotonic() > deadline:
                        raise MailUnavailable("response_time_bound")
                    body.extend(chunk)
                    if len(body) > 1048576:
                        raise MailUnavailable("response_too_large")
            if time.monotonic() > deadline:
                raise MailUnavailable("response_time_bound")
            value = json.loads(body)
        except httpx.HTTPError:
            raise MailUnavailable("http_transport_failed") from None
        except (ValueError, UnicodeError):
            raise MailUnavailable("invalid_provider_json") from None
        if not isinstance(value, dict):
            raise MailUnavailable("invalid_provider_shape")
        return value

    def _connect(self):
        if self._attempted:
            if not self._ready or time.monotonic() >= self._expires:
                raise MailUnavailable("connection_unavailable")
            return
        self._attempted = True
        token = self._json("POST", "https://oauth2.googleapis.com/token", data=self._credentials)
        if frozenset(str(token.get("scope", "")).split()) != SCOPES:
            raise MailUnavailable("scope_mismatch")
        access = token.get("access_token")
        expires = token.get("expires_in")
        if (
            not isinstance(access, str)
            or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,4096}", access)
            or token.get("token_type", "").lower() != "bearer"
            or type(expires) is not int
            or not 60 <= expires <= 86400
        ):
            raise MailUnavailable("invalid_access_token_response")
        self._token = access
        profile = self._json("GET", ROOT + "/profile", headers=self._headers())
        if profile.get("emailAddress", "").lower() != OWNER:
            raise MailUnavailable("mailbox_mismatch")
        self._expires = time.monotonic() + min(expires, 3600) - 30
        self._ready = True

    def _headers(self):
        return {"Authorization": "Bearer " + self._token}

    def find_sent(self, route):
        if route not in self._recipients:
            raise ValueError("route outside original pilot")
        query = "in:sent to:" + self._recipients[route] + " after:2026/10/18 before:2026/10/21"
        self._connect()
        return self._json(
            "GET", ROOT + "/threads", headers=self._headers(), params={"q": query, "maxResults": 5}
        )

    def thread(self, thread_id):
        _thread_id(thread_id)
        self._connect()
        value = self._json(
            "GET", ROOT + "/threads/" + thread_id, headers=self._headers(), params={"format": "full"}
        )
        if value.get("id") != thread_id or not isinstance(value.get("messages"), list):
            raise MailUnavailable("invalid_thread_response")
        return value

    def send_reply(self, route, thread_id, mail):
        _thread_id(thread_id)
        if (
            route not in self._recipients
            or _addresses(mail, "To") != [self._recipients[route].lower()]
            or _addresses(mail, "From") != [OWNER]
            or any(mail.get_all(header) for header in ("Cc", "Bcc", "Resent-To", "Resent-Cc", "Resent-Bcc"))
            or len(mail.get_all("In-Reply-To", [])) != 1
            or not re.fullmatch(r"<[^\s<>@]{1,160}@[^\s<>@]{1,90}>", mail["In-Reply-To"])
            or not mail.get("References")
            or mail.get("Auto-Submitted") != "auto-replied"
        ):
            raise ValueError("unsafe reply envelope")
        raw = mail.as_bytes(policy=policy.SMTP)
        if len(raw) > 524288:
            raise ValueError("reply MIME too large")
        self._connect()
        receipt = self._json(
            "POST",
            ROOT + "/messages/send",
            headers=self._headers(),
            json={"threadId": thread_id, "raw": base64.urlsafe_b64encode(raw).decode("ascii")},
        )
        message_id = receipt.get("id")
        if (
            not isinstance(message_id, str)
            or not re.fullmatch(r"[A-Fa-f0-9]{1,128}", message_id)
            or receipt.get("threadId") != thread_id
        ):
            raise MailUnavailable("ambiguous_send_receipt")
        return {"id": message_id, "threadId": thread_id}
