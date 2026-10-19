"""Pure fixtures: no mailbox, customer, order or outbound message is created."""

import pytest

from pv_growth.acquisition.reply_policy import reply_decision


def message(body="بله علاقه‌مندم، راهنمایی کنید", **headers):
    values = {
        "From": "Shop <shop@example.org>",
        "To": "pvnetwork8@gmail.com",
        "Subject": "Re: proposal | S01-261019",
        "Authentication-Results": "mx.google.com; dmarc=pass header.from=example.org",
        **headers,
    }
    return {
        "text": body,
        "headers": [{"name": k, "value": v} for k, v in values.items()],
        "labels": ["INBOX"],
    }


def decide(item):
    return reply_decision(item, recipient="shop@example.org", owner="pvnetwork8@gmail.com")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("بله علاقه‌مندم، راهنمایی کنید", "interest"),
        ("قیمت پلن‌ها چقدره؟", "price"),
        ("چطور پرداخت کنم؟", "payment"),
        ("برای نصب روی آیفون راهنمایی دارید؟", "setup"),
        ("پرداخت کردم، الان چه کنم؟", "payment_reported"),
        ("چه نوع همکاری مد نظرتونه؟", "collaboration"),
        ("لطفاً تخفیف پنجاه درصد بدید", "hold_terms"),
        ("بله ولی فعلاً تمایل ندارم، پیام نفرستید", "stop"),
        ("Not interested, please unsubscribe", "stop"),
        ("مرسی", "hold_intent"),
        ("", "hold_intent"),
        ("نه ممنون", "stop"),
    ],
)
def test_only_explicit_supported_intents_can_be_answered(body, expected):
    assert decide(message(body)) == expected


@pytest.mark.parametrize(
    "body",
    [
        "ممنون\nOn Mon, PV Network wrote:\nقیمت پلن و خرید اشتراک",
        "\n> قیمت پلن‌ها چقدره؟\n> خرید کنید",
        "متوجه نشدم\nدر تاریخ دوشنبه نوشت:\nبله علاقه‌مندم",
        "سوال نامشخص\n-----Original Message-----\nقیمت و خرید",
    ],
)
def test_quoted_outbound_copy_cannot_trigger_a_reply(body):
    assert decide(message(body)) == "hold_intent"


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Auto-Submitted": "auto-generated"},
        {"Precedence": "bulk"},
        {"List-Id": "offers.example.org"},
        {"Content-Type": "multipart/report; report-type=delivery-status"},
        {"From": "Mail Delivery System <mailer-daemon@example.org>"},
        {"X-Autoreply": "yes"},
    ],
)
def test_delivery_and_automatic_messages_cannot_generate_mail(headers):
    assert decide(message("بله علاقه‌مندم", **headers)).startswith("ignore_")


@pytest.mark.parametrize(
    "headers",
    [
        {"Reply-To": "other@example.org"},
        {"From": "other@example.org"},
        {"From": "shop@example.org, other@example.org"},
        {"Reply-To": "shop@example.org, other@example.org"},
        {"Authentication-Results": "mx.google.com; dmarc=fail header.from=example.org"},
        {"Authentication-Results": "attacker.invalid; dmarc=pass header.from=example.org"},
        {"Authentication-Results": "mx.google.com; dmarc=pass header.from=other.org"},
        {"Authentication-Results": ""},
    ],
)
def test_unauthenticated_sender_or_changed_reply_destination_is_held(headers):
    assert decide(message(**headers)).startswith("hold_")


def test_injected_second_authentication_header_does_not_override_google_failure():
    item = message(**{"Authentication-Results": "mx.google.com; dmarc=fail header.from=example.org"})
    item["headers"].append(
        {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass header.from=example.org"}
    )
    assert decide(item) == "hold_authentication"


def test_spam_and_own_messages_do_not_trigger_replies():
    item = message()
    item["labels"] = ["SPAM"]
    assert decide(item) == "ignore_spam"
    assert decide(message(**{"From": "pvnetwork8@gmail.com"})) == "ignore_self"


def test_positive_words_with_unclear_negation_do_not_trigger_an_answer():
    assert decide(message("شاید بعداً خرید کنم، الان نه")) == "hold_intent"
