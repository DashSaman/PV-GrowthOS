from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from pv_growth.acquisition.mail_budget import KEY, finish, initialize, reserve
from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig

NOW = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)


def start(settings, total=20, initial=20):
    initialize(
        settings,
        observed_sent=[f"{i + 1:x}" for i in range(total)],
        observed_at=NOW,
        initial_already_attempted=initial,
        now=NOW,
    )


def take(settings, op, kind="initial", total=20, at=NOW, now=NOW):
    return reserve(
        settings, op, kind, observed_sent=[f"{i + 1:x}" for i in range(total)], observed_at=at, now=now
    )


def test_cap_counts_existing_twenty_initials(settings):
    start(settings)
    for i in range(430):
        assert take(settings, f"op-{i}") is not None
    assert take(settings, "over-initial") is None
    assert take(settings, "reply-one", "reply") is not None


def test_all_campaign_replies_and_unknown_attempts_share500(settings):
    start(settings, total=498)
    assert take(settings, "old30-human", "reply", 498) is not None
    assert take(settings, "new430-first", "initial", 498) is not None
    assert take(settings, "needs20-human", "reply", 498) is None
    assert take(settings, "another-new", "initial", 498) is None


def test_duplicate_or_crashed_operation_cannot_reserve_twice(settings):
    start(settings)
    assert take(settings, "crash-before-post") is not None
    assert take(settings, "crash-before-post") is None
    with session_scope(settings) as db:
        assert db.get(AppConfig, KEY).value["attempts"] == 1


def test_fresh_manual_sent_observation_reduces_remaining_budget(settings):
    start(settings)
    assert take(settings, "external-higher", total=499) is not None
    assert take(settings, "cannot-ignore-manual", total=499) is None


def test_budget_missing_or_stale_history_fails_closed(settings):
    assert take(settings, "missing") is None
    start(settings)
    assert take(settings, "stale", at=NOW - timedelta(seconds=31)) is None
    assert take(settings, "future", at=NOW + timedelta(seconds=1)) is None
    with session_scope(settings) as db:
        assert db.get(AppConfig, KEY).value["attempts"] == 0


def test_no_reset_of_existing_budget_is_possible(settings):
    start(settings)
    with pytest.raises(ValueError):
        start(settings, total=0, initial=0)


def test_two_concurrent_campaigns_cannot_take_last_budget_unit(settings):
    start(settings, total=499)
    gate = Barrier(2)

    def run(i):
        gate.wait(timeout=5)
        return take(settings, f"racing-{i}", "reply", 499)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(x is not None for x in results) == 1


@pytest.mark.parametrize("count", [-1, 501, True, None])
def test_invalid_provider_counts_never_initialize(settings, count):
    with pytest.raises((ValueError, TypeError)):
        initialize(settings, observed_sent=count, observed_at=NOW, initial_already_attempted=20, now=NOW)


def test_verified_receipts_and_sent_index_are_not_counted_twice(settings):
    start(settings, total=498)
    a = take(settings, "one-more", "reply", 498)
    assert a
    finish(settings, a, "1f3")
    assert take(settings, "second-more", "reply", 499) is not None
    assert take(settings, "third-more", "reply", 499) is None


def test_rolling_window_expires_charge_but_never_releases_operation(settings):
    start(settings, total=499)
    assert take(settings, "yesterday", "reply", 499)
    later = NOW + timedelta(days=1, seconds=1)
    assert take(settings, "yesterday", "reply", 0, at=later, now=later) is None
    assert take(settings, "fresh-human", "reply", 0, at=later, now=later) is not None


def test_receipt_cannot_be_changed_or_refunded(settings):
    start(settings)
    key = take(settings, "receipt")
    finish(settings, key, "abcdef")
    with pytest.raises(ValueError):
        finish(settings, key, "abcdff")
