from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from pv_growth.acquisition import mail_api_quota as quota

NOW = datetime(2026, 10, 25, tzinfo=UTC)
ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"


@pytest.mark.parametrize(
    ("method", "path", "params", "cost"),
    [
        ("GET", "/profile", None, 1),
        ("GET", "/threads", None, 10),
        ("GET", "/threads/abc", {"format": "full"}, 120),
        ("GET", "/threads/abc", {"format": "minimal"}, 40),
        ("GET", "/messages", None, 5),
        ("GET", "/messages/abc", {"format": "raw"}, 60),
        ("GET", "/messages/abc", {"format": "metadata"}, 20),
        ("GET", "/messages/abc/attachments/def", None, 20),
        ("POST", "/messages/send", None, 100),
    ],
)
def test_current_gmail_quota_weights(method, path, params, cost):
    assert quota.units(method, ROOT + path, params) == cost


def test_oauth_refresh_is_not_a_gmail_request():
    assert quota.units("POST", "https://oauth2.googleapis.com/token", None) is None


def test_workers_share_persisted_rate_slots_without_touching_mail_claims(settings):
    assert quota.reserve(settings, 120, NOW) == 0
    assert quota.reserve(settings, 120, NOW) == 4
    assert quota.reserve(settings, 120, NOW + timedelta(seconds=9)) == 0


def test_parallel_workers_cannot_reserve_the_same_slot(settings):
    with ThreadPoolExecutor(max_workers=5) as pool:
        delays = list(pool.map(lambda _: quota.reserve(settings, 120, NOW), range(5)))
    assert sorted(delays) == [0, 4, 8, 12, 16]


def test_invalid_or_unbounded_reservation_fails_closed(settings):
    for bad in (0, -1, 121, True):
        with pytest.raises(ValueError):
            quota.reserve(settings, bad, NOW)
    for _ in range(16):
        quota.reserve(settings, 120, NOW)
    with pytest.raises(ValueError):
        quota.reserve(settings, 120, NOW)
