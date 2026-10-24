"""Fixed-endpoint Gmail transport for the original thirty-contact pilot.

No redirects, retries, mailbox mutation or logging of provider responses. The
processor must verify the original thread and commit its durable send claim
before invoking send_reply; a send exception always consumes that claim.
"""

import base64
import hashlib
import json
import re
import time
from datetime import timedelta
from email import policy
from email.utils import getaddresses

import httpx

from pv_growth.acquisition import mail_budget
from pv_growth.acquisition.reply_ledger import EXPANSION_ROUTES, MAPS_ROUTES, NEEDS_ROUTES, ROUTES
from pv_growth.core.config import get_settings
from pv_growth.database.types import utcnow

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
    _routes = ROUTES
    _sent_after = "2026/10/18"
    _sent_before = "2026/10/21"

    def __init__(
        self,
        *,
        client_id,
        client_secret,
        refresh_token,
        recipients,
        http_transport=None,
        budget_settings=None,
    ):
        if (
            not isinstance(recipients, dict)
            or set(recipients) != self._routes
            or len(set(recipients.values())) != len(self._routes)
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
        self._budget_settings = budget_settings
        self._prepared_budget = {}
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
        self._prepared_budget.clear()

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
        query = (
            "in:sent to:"
            + self._recipients[route]
            + " after:"
            + self._sent_after
            + " before:"
            + self._sent_before
        )
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

    def sent_snapshot(self):
        self._connect()
        cutoff = int((utcnow() - timedelta(days=1)).timestamp())
        value = self._json(
            "GET",
            ROOT + "/messages",
            headers=self._headers(),
            params={"q": "in:sent after:" + str(cutoff), "includeSpamTrash": "true", "maxResults": 500},
        )
        messages = value.get("messages", [])
        if (
            value.get("nextPageToken")
            or not isinstance(messages, list)
            or any(not isinstance(m, dict) for m in messages)
        ):
            raise MailUnavailable("shared_mail_snapshot_incomplete")
        ids = [m.get("id") for m in messages]
        try:
            mail_budget._snapshot(ids)
        except (ValueError, TypeError):
            raise MailUnavailable("shared_mail_snapshot_invalid") from None
        return ids, utcnow()

    def _reply_raw(self, route, thread_id, mail):
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
        if self._budget_settings is None and get_settings().env == "production":
            raise MailUnavailable("production_transport_budget_missing")
        raw = mail.as_bytes(policy=policy.SMTP)
        if len(raw) > 524288:
            raise ValueError("reply MIME too large")
        return raw

    def _prepare_budget(self, raw):
        self._connect()
        if self._budget_settings is None:
            return True
        key = hashlib.sha224(raw).hexdigest()
        if key in self._prepared_budget:
            return True
        ids, observed = self.sent_snapshot()
        claim = mail_budget.reserve(
            self._budget_settings,
            "reply:" + key,
            "reply",
            observed_sent=ids,
            observed_at=observed,
            now=utcnow(),
        )
        if claim is None:
            return False
        self._prepared_budget[key] = claim
        return True

    def prepare_reply(self, route, thread_id, mail):
        """Reserve shared quota before the processor consumes its reply phase."""
        return self._prepare_budget(self._reply_raw(route, thread_id, mail))

    def send_reply(self, route, thread_id, mail):
        raw = self._reply_raw(route, thread_id, mail)
        if not self._prepare_budget(raw):
            raise MailUnavailable("shared_mail_budget_held")
        budget_claim = self._prepared_budget.pop(hashlib.sha224(raw).hexdigest(), None)
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
        if budget_claim is not None:
            mail_budget.finish(self._budget_settings, budget_claim, message_id)
        return {"id": message_id, "threadId": thread_id}


class MapsGmailTransport(GmailTransport):
    """Same fixed Gmail boundary, restricted to the independently approved ten."""

    _routes = MAPS_ROUTES
    _sent_after = "2026/10/19"
    _sent_before = "2026/10/22"


class NeedsGmailTransport(GmailTransport):
    """Exact twenty-route needs-led campaign, isolated from both previous pilots."""

    _routes = NEEDS_ROUTES
    _sent_after = "2026/10/23"
    _sent_before = "2026/10/27"


class ExpansionGmailTransport(GmailTransport):
    """Frozen registered batches within430additional routes; same shared budget."""

    _routes = EXPANSION_ROUTES

    def __init__(self, *, recipients, **kwargs):
        if (
            not isinstance(recipients, dict)
            or not 1 <= len(recipients) <= 430
            or not set(recipients) <= EXPANSION_ROUTES
        ):
            raise ValueError("expansion routes outside approved cap")
        self._routes = frozenset(recipients)
        super().__init__(recipients=recipients, **kwargs)

    _sent_after = "2026/10/23"
    _sent_before = "2026/10/27"
