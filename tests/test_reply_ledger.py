"""Real database transactions; no Gmail, customer or order side effects."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from pv_growth.acquisition.reply_ledger import claim_reply, finish_reply, stop_route
from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig


def test_reopening_the_database_after_a_reserved_send_never_replays(settings):
    assert claim_reply(settings, "a01", "gmail-001", "interest") is not None
    # A crashed sender cannot release the durable claim, even without a receipt.
    assert claim_reply(settings, "a01", "gmail-001", "interest") is None


@pytest.mark.parametrize("result", ["sent", "unknown"])
def test_sent_or_ambiguous_send_stays_non_replayable(settings, result):
    claim = claim_reply(settings, "s20", "gmail-002", "price")
    finish_reply(settings, claim, result)
    assert claim_reply(settings, "s20", "gmail-002", "price") is None


def test_new_inbound_does_not_repeat_the_same_phase_message(settings):
    assert claim_reply(settings, "s01", "gmail-003", "payment") is not None
    assert claim_reply(settings, "s01", "gmail-004", "payment") is None
    assert claim_reply(settings, "s01", "gmail-005", "setup") is not None


def test_one_inbound_cannot_be_used_for_two_routes_or_intents(settings):
    assert claim_reply(settings, "s01", "gmail-006", "interest") is not None
    assert claim_reply(settings, "a02", "gmail-006", "price") is None


def test_two_concurrent_polls_get_only_one_send_claim(settings):
    gate = Barrier(2)

    def contender():
        gate.wait(timeout=5)
        return claim_reply(settings, "a10", "gmail-007", "interest")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: contender(), range(2)))
    assert sum(item is not None for item in results) == 1


def test_stop_is_durable_across_new_incoming_messages(settings):
    stop_route(settings, "s02")
    assert claim_reply(settings, "s02", "gmail-008", "interest") is None
    assert claim_reply(settings, "s02", "gmail-009", "price") is None


def test_cancelled_before_send_reservation_is_never_released(settings):
    claim = claim_reply(settings, "s02", "gmail-held", "price")
    finish_reply(settings, claim, "held")
    assert claim_reply(settings, "s02", "gmail-held", "price") is None


@pytest.mark.parametrize("route", ["a00", "a11", "s21", "held", "new-campaign"])
def test_no_route_outside_original_thirty_can_reserve_mail(settings, route):
    with pytest.raises(ValueError):
        claim_reply(settings, route, "gmail-010", "interest")


@pytest.mark.parametrize("intent", ["stop", "hold_terms", "ignore_automatic", "discount"])
def test_hold_stop_or_unapproved_actions_cannot_reserve_a_send(settings, intent):
    with pytest.raises(ValueError):
        claim_reply(settings, "a01", "gmail-011", intent)


def test_duplicate_phase_rollback_does_not_poison_a_different_intent(settings):
    assert claim_reply(settings, "s03", "gmail-012", "interest") is not None
    assert claim_reply(settings, "s03", "gmail-013", "interest") is None
    assert claim_reply(settings, "s03", "gmail-013", "payment") is not None


def test_ledger_stores_only_route_phase_and_fingerprints(settings):
    claim = claim_reply(settings, "s04", "gmail-014", "setup")
    finish_reply(settings, claim, "sent")
    with session_scope(settings) as db:
        rows = list(db.scalars(select(AppConfig)))
        assert len(rows) == 2
        serialized = str([(row.key, row.value) for row in rows])
        assert "gmail-014" not in serialized
        assert "@" not in serialized
        assert all(row.value["state"] == "sent" for row in rows)


def test_final_receipt_cannot_be_changed_to_release_or_retry(settings):
    claim = claim_reply(settings, "s05", "gmail-015", "interest")
    finish_reply(settings, claim, "unknown")
    with pytest.raises(ValueError):
        finish_reply(settings, claim, "sent")
    assert claim_reply(settings, "s05", "gmail-015", "interest") is None
