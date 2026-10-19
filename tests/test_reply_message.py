import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from pv_growth.acquisition.reply_message import build_reply

NOW = datetime(2026, 10, 19, 18, tzinfo=UTC)


def catalogue():
    return {
        "source": "mirza_local_readonly", "observed_at_utc": NOW.isoformat(),
        "plans": [{"code": "public", "name": "۲۰ گیگ", "volume_gb": 20,
                   "duration_days": 31, "price_toman": 100000, "location": "تانل"}],
        "payment": {"direct_buy": False, "minimum_wallet_toman": 100000,
                    "card_to_support": True, "support_handle": "PVNetwork1"},
    }


def build(**updates):
    values = {"route": "a01", "recipient": "shop@example.org", "name": "فروشگاه",
              "subject": "پیشنهاد | A01-261019", "intent": "price", "inbound_id": "gmail-001",
              "in_reply_to": "<human@example.org>", "references": ["<original@mail.gmail.com>"],
              "catalogue": catalogue(), "now": NOW}
    return build_reply(**{**values, **updates})


def test_brand_and_original_logo_are_embedded_without_remote_image_fetch():
    mail = build()
    html = mail.get_body(preferencelist=("html",)).get_content()
    assert "#080604" in html and "#e6b84b" in html and 'dir="rtl"' in html
    images = [part for part in mail.walk() if part.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<pvnetwork-logo>"
    assert hashlib.sha256(images[0].get_payload(decode=True)).hexdigest() == (
        "189fa0ce9f9496c321f2ec411cbe1eabbce45438d5dfc3931917c93dae225ae3"
    )
    assert 'src="cid:pvnetwork-logo"' in html
    assert 'src="http' not in html


def test_route_is_retained_in_both_alternatives_and_no_placeholder_is_sent():
    mail = build(route="s20")
    plain = mail.get_body(preferencelist=("plain",)).get_content()
    html = mail.get_body(preferencelist=("html",)).get_content()
    for body in (plain, html):
        assert "https://t.me/pvgrowthos_bot?start=social_ext261019_s20" in body
        assert "{{" not in body
        assert "buy_share" not in body
    assert "100,000" in plain


def test_header_and_html_boundaries_are_escaped_and_reply_is_single_recipient():
    mail = build(name='<script>alert("x")</script>')
    html = mail.get_body(preferencelist=("html",)).get_content()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert mail["To"] == "shop@example.org" and mail["Cc"] is None and mail["Bcc"] is None
    assert mail["Auto-Submitted"] == "auto-replied"
    assert mail["In-Reply-To"] == "<human@example.org>"
    assert mail["References"] == "<original@mail.gmail.com> <human@example.org>"


@pytest.mark.parametrize("updates", [
    {"recipient": "shop@example.org\nBcc:other@example.org"},
    {"recipient": "shop@example.org, other@example.org"},
    {"subject": "hello\r\nBcc:other@example.org"},
    {"in_reply_to": "bad\nheader"},
    {"references": ["bad\nheader"]},
    {"route": "s21"},
    {"intent": "hold_terms"},
    {"intent": "ignore_automatic"},
    {"catalogue": None},
    {"now": NOW + timedelta(minutes=91)},
])
def test_unsafe_or_unverified_input_cannot_become_outbound_mime(updates):
    with pytest.raises(ValueError):
        build(**updates)


def test_reported_payment_only_guides_to_manual_support_and_never_confirms_payment():
    mail = build(intent="payment_reported")
    plain = mail.get_body(preferencelist=("plain",)).get_content()
    assert "تأیید" in plain and "پشتیبانی" in plain and "دوباره پرداخت نکن" in plain
    assert "پرداخت شما تأیید شد" not in plain
