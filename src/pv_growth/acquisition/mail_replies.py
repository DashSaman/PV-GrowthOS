"""Replies to genuine inbound messages in the original pilot, never reminders.

Production callers must hold the campaign advisory lock. Purchase attribution
remains exclusively in the existing outreach report and its test exclusions.
"""

import base64
import html
import json
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from hashlib import sha256

from pv_growth.acquisition.mail_transport import (
    OWNER,
    GmailTransport,
    MailUnavailable,
    MapsGmailTransport,
    NeedsGmailTransport,
)
from pv_growth.acquisition.reply_ledger import (
    INTENTS,
    MAPS_CAMPAIGN,
    NEEDS_CAMPAIGN,
    claim_reply,
    finish_reply,
    routes_for,
    source_url,
    stop_route,
)
from pv_growth.acquisition.reply_message import build_reply
from pv_growth.acquisition.reply_policy import reply_decision
from pv_growth.acquisition.retail_catalogue import validate_catalogue
from pv_growth.mirza_adapter.retail_sales import _classification, aware

CAMPAIGN = "external-acquisition-20261019"
START = datetime(2026, 10, 19, tzinfo=UTC)
END = datetime(2026, 11, 19, tzinfo=UTC)
PILOT_FINGERPRINT = "59a08bcd08d035b3d3d548a6a834554b18f36aaa7b9628ad20632df95e7cc37a"
MAPS_START = datetime(2026, 10, 20, tzinfo=UTC)
MAPS_END = datetime(2026, 11, 20, tzinfo=UTC)
MAPS_PILOT_FINGERPRINT = "ce95efbd67a79a6f33ab9e2c9c37a3c84cfa5a7c2d5c4e619f0c638834045d26"

NEEDS_START = datetime(2026, 10, 24, tzinfo=UTC)
NEEDS_END = datetime(2026, 11, 24, tzinfo=UTC)
NEEDS_PILOT_FINGERPRINT = "beb3f6ab3207ce96840ca06aa93731cbdca16a11a904537b1cf679934e00125f"


def _pilot(campaign):
    routes_for(campaign)
    if campaign == NEEDS_CAMPAIGN:
        return (
            NEEDS_START,
            NEEDS_END,
            NEEDS_PILOT_FINGERPRINT,
            NeedsGmailTransport,
            "retail-needs:20261024:latest",
        )
    if campaign == MAPS_CAMPAIGN:
        return MAPS_START, MAPS_END, MAPS_PILOT_FINGERPRINT, MapsGmailTransport, "retail-maps:20261020:latest"
    return START, END, PILOT_FINGERPRINT, GmailTransport, "retail-outreach:20261019:latest"


def verify_pilot(routes, *, campaign=CAMPAIGN):
    if set(routes) != routes_for(campaign):
        raise ValueError("pilot route mismatch")
    rows = [[k, r["email"].lower(), r["subject"]] for k, r in sorted(routes.items())]
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if sha256(encoded).hexdigest() != _pilot(campaign)[2]:
        raise ValueError("pilot recipient fingerprint mismatch")


def validate_config(value, now, *, campaign=CAMPAIGN):
    try:
        start, end, _fingerprint, transport, _sales = _pilot(campaign)
        expected = routes_for(campaign)
        until = datetime.fromisoformat(value["authorized_until_utc"])
        rows = value["recipients"]
        if (
            value["campaign"] != campaign
            or value["owner"] != OWNER
            or until.tzinfo is None
            or not start <= aware(now) < until <= end
            or not isinstance(rows, list)
            or len(rows) != len(expected)
            or {row["route"] for row in rows} != expected
            or any(
                not isinstance(row["name"], str)
                or len(row["name"]) > 160
                or not isinstance(row["subject"], str)
                or not 1 <= len(row["subject"]) <= 180
                or any(ord(c) < 32 or ord(c) == 127 for c in row["subject"])
                for row in rows
            )
            or set(value["credentials"]) != {"client_id", "client_secret", "refresh_token"}
        ):
            raise ValueError
        # This validates credentials/recipients locally; it makes no HTTP call.
        with transport(**value["credentials"], recipients={r["route"]: r["email"] for r in rows}):
            pass
        return {row["route"]: dict(row) for row in rows}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("invalid or expired private pilot configuration") from None


def _headers(message):
    rows = message.get("payload", {}).get("headers", [])
    if not isinstance(rows, list) or len(rows) > 150:
        raise ValueError("invalid headers")
    values = {}
    for row in rows:
        key, value = row.get("name"), row.get("value")
        if not isinstance(key, str) or not isinstance(value, str) or len(value) > 12000:
            raise ValueError("invalid header")
        values.setdefault(key.lower(), []).append(value)
    return values


def _one(values, key):
    rows = values.get(key, [])
    return rows[0] if len(rows) == 1 else ""


def _address(values, key):
    return [address.lower() for _, address in getaddresses([_one(values, key)])]


def _body(part, *, original=False, depth=0):
    if depth > 6 or not isinstance(part, dict) or part.get("filename"):
        return ""
    kind = part.get("mimeType", "")
    if kind == "text/plain" or (original and kind == "text/html"):
        data = part.get("body", {}).get("data", "")
        if not isinstance(data, str) or len(data) > 32768:
            raise ValueError("body unavailable")
        try:
            raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
            return raw.decode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("body encoding unavailable") from None
    parts = part.get("parts", [])
    if not isinstance(parts, list) or len(parts) > 30:
        raise ValueError("body part bound exceeded")
    result = "\n".join(_body(p, original=original, depth=depth + 1) for p in parts)
    if len(result) > 24000:
        raise ValueError("body bound exceeded")
    return result


def _ordered(thread, tid, now, *, campaign=CAMPAIGN):
    rows = thread.get("messages")
    if thread.get("id") != tid or not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("thread unavailable")
    seen, ordered = set(), []
    for row in rows:
        mid, stamp = row.get("id"), int(row.get("internalDate", "0"))
        if (
            not isinstance(mid, str)
            or not re.fullmatch(r"[A-Fa-f0-9]{1,128}", mid)
            or mid in seen
            or row.get("threadId") != tid
            or not int(_pilot(campaign)[0].timestamp() * 1000) <= stamp <= int(aware(now).timestamp() * 1000)
        ):
            raise ValueError("thread identity unavailable")
        seen.add(mid)
        ordered.append((stamp, mid, row))
    return [row for _, _, row in sorted(ordered)]


def _original(rows, route, *, campaign=CAMPAIGN):
    source = source_url(campaign, route["route"])
    for row in rows:
        headers = _headers(row)
        if (
            "SENT" in row.get("labelIds", [])
            and _address(headers, "from") == [OWNER]
            and _address(headers, "to") == [route["email"].lower()]
            and not headers.get("cc")
            and not headers.get("bcc")
            and _one(headers, "subject") == route["subject"]
            and re.search(
                re.escape(source) + r"""(?=$|[\s<>"'])""", html.unescape(_body(row["payload"], original=True))
            )
        ):
            return row
    return None


def _decision(message, recipient):
    headers = _headers(message)
    if _address(headers, "to") != [OWNER]:
        return "hold_recipient"
    return reply_decision(
        {
            "headers": message["payload"]["headers"],
            "labels": message.get("labelIds", []),
            "text": _body(message["payload"]),
        },
        recipient=recipient,
        owner=OWNER,
    )


def _inspect(thread, tid, route, now, *, campaign=CAMPAIGN):
    rows = _ordered(thread, tid, now, campaign=campaign)
    original = _original(rows, route, campaign=campaign)
    if original is None:
        return "held_original", None
    after = rows[rows.index(original) + 1 :]
    for row in after:
        if _decision(row, route["email"]) == "stop":
            return "stopped", None
    if not after or _address(_headers(after[-1]), "from") == [OWNER]:
        return "held_no_new_human_reply", None
    return _decision(after[-1], route["email"]), after[-1]


def process(settings, api, *, config, catalogue, classification, now, lock_held=False, campaign=CAMPAIGN):
    """One bounded cycle; a failure never becomes silence, delivery or a sale."""
    if settings.env == "production" and not lock_held:
        raise ValueError("campaign lock required")
    routes = validate_config(config, now, campaign=campaign)
    if settings.env == "production":
        verify_pilot(routes, campaign=campaign)
    state, tests, _ = _classification(classification)
    if state != "owner_confirmed" or len(tests) < 3 or validate_catalogue(catalogue, now=now) is None:
        raise ValueError("catalogue or confirmed test classification unavailable")
    started = time.monotonic()
    deadline = started + 120
    result = {
        "campaign": campaign,
        "observed_at_utc": aware(now).isoformat(),
        "status": "ok",
        "data_complete": True,
        "routes": {},
        "payment_confirmation": "manual_support",
        "execution_owner": "docker_scheduler",
        "sales_report_key": _pilot(campaign)[4],
        "confirmed_internal_test_account_count": len(tests),
        "reminders_sent": 0,
    }
    for key, route in sorted(routes.items()):
        if time.monotonic() >= deadline:
            result.update(status="partial", data_complete=False)
            result["routes"][key] = "not_visited_time_bound"
            continue
        try:
            found = api.find_sent(key)
            candidates = found.get("threads", [])
            if found.get("nextPageToken") or not isinstance(candidates, list) or len(candidates) != 1:
                result["routes"][key] = "held_original_lookup"
                continue
            tid = candidates[0]["id"]
            decision, incoming = _inspect(api.thread(tid), tid, route, now, campaign=campaign)
            if decision == "stopped":
                stop_route(settings, key, campaign=campaign)
            if decision not in INTENTS:
                result["routes"][key] = decision
                continue
            headers = _headers(incoming)
            references = _one(headers, "references").split()
            current = aware(now) + timedelta(seconds=time.monotonic() - started)
            mail = build_reply(
                route=key,
                recipient=route["email"],
                name=route["name"],
                subject=route["subject"],
                intent=decision,
                inbound_id=incoming["id"],
                in_reply_to=_one(headers, "message-id"),
                references=references[-9:],
                catalogue=catalogue,
                now=current,
                campaign=campaign,
            )
            claim = claim_reply(settings, key, incoming["id"], decision, campaign=campaign)
            if claim is None:
                result["routes"][key] = "held_duplicate"
                continue
            # A committed reservation survives a crash even during this final read.
            send_attempted = False
            try:
                latest_decision, latest = _inspect(api.thread(tid), tid, route, now, campaign=campaign)
                if latest_decision == "stopped":
                    stop_route(settings, key, campaign=campaign)
                if latest_decision != decision or latest is None or latest["id"] != incoming["id"]:
                    finish_reply(settings, claim, "held")
                    result["routes"][key] = (
                        "stopped" if latest_decision == "stopped" else "held_thread_changed"
                    )
                    continue
                current = aware(now) + timedelta(seconds=time.monotonic() - started)
                if time.monotonic() >= deadline or validate_catalogue(catalogue, now=current) is None:
                    raise ValueError("catalogue expired during poll")
                send_attempted = True
                api.send_reply(key, tid, mail)
            except Exception:
                finish_reply(settings, claim, "unknown" if send_attempted else "held")
                result["routes"][key] = "unknown_send" if send_attempted else "held_before_send"
                result.update(status="partial", data_complete=False)
                break
            finish_reply(settings, claim, "sent")
            result["routes"][key] = "sent"
        except MailUnavailable:
            result["routes"][key] = "unavailable_transport"
            result.update(status="partial", data_complete=False)
            break
        except (ValueError, KeyError, TypeError, AttributeError):
            result["routes"][key] = "held_malformed_thread"
    for key in routes:
        result["routes"].setdefault(key, "not_visited_after_error")
    return result
