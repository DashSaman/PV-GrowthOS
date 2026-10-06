import importlib
import importlib.util
from datetime import date

import pytest

from pv_growth.core.errors import ValidationError


def module():
    assert importlib.util.find_spec("pv_growth.free_config.budgets"), "durable byte budget is missing"
    return importlib.import_module("pv_growth.free_config.budgets")


def test_random_quotas_fill_fifty_gib_without_unlimited_or_overshoot():
    quotas = module().allocate_quotas(100, 50 * 1024**3)
    assert len(quotas) == 100
    assert sum(quotas) == 50 * 1024**3
    assert all(50 * 1024**2 <= q <= 1024**3 for q in quotas)
    assert len(set(quotas)) > 1


def test_fewer_entrants_never_invent_winners():
    quotas = module().allocate_quotas(3, 50 * 1024**3)
    assert len(quotas) == 3
    assert sum(quotas) <= 3 * 1024**3


def test_few_winners_still_get_random_volumes(monkeypatch):
    b = module()
    monkeypatch.setattr(b.secrets, "randbelow", lambda bound: 0)
    assert b.allocate_quotas(3, 50 * b.GIB) == [50 * b.MIB] * 3


def test_reserved_failed_traffic_still_consumes_daily_budget(session):
    b = module()
    day = date(2026, 10, 6)
    first = b.reserve(session, key="first", day=day, bucket="public", traffic_bytes=1024**3)
    first.status = "provision_failed"
    session.flush()
    for i in range(9):
        b.reserve(session, key=f"next{i}", day=day, bucket="public", traffic_bytes=1024**3)
    with pytest.raises(ValidationError):
        b.reserve(session, key="overshoot", day=day, bucket="public", traffic_bytes=1024**3)


def test_budget_replay_never_reserves_twice_and_cannot_change_quota(session):
    b = module()
    day = date(2026, 10, 6)
    one = b.reserve(session, key="same", day=day, bucket="lottery", traffic_bytes=50 * 1024**2)
    two = b.reserve(session, key="same", day=day, bucket="lottery", traffic_bytes=50 * 1024**2)
    assert one.id == two.id
    with pytest.raises(ValidationError):
        b.reserve(session, key="same", day=day, bucket="lottery", traffic_bytes=1024**3)


def test_public_and_lottery_budgets_are_separate(session):
    b = module()
    day = date(2026, 10, 6)
    for i in range(10):
        b.reserve(session, key=f"public{i}", day=day, bucket="public", traffic_bytes=1024**3)
    grant = b.reserve(session, key="private", day=day, bucket="lottery", traffic_bytes=1024**3)
    assert grant.bucket == "lottery"


def test_postgres_byte_budget_is_serialized_across_workers(session, settings):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from pv_growth.database.base import session_scope
    from pv_growth.database.models import FreeAllocation

    if os.environ.get("TEST_DB") != "postgres":
        pytest.skip("actual PostgreSQL row locking is exercised in CI")
    b = module()
    day = date(2026, 12, 31)
    for i in range(9):
        b.reserve(session, key=f"filled{i}", day=day, bucket="public", traffic_bytes=b.GIB)
    session.commit()
    barrier = Barrier(2)

    def attempt(key):
        with session_scope(settings) as worker:
            barrier.wait(timeout=5)
            try:
                b.reserve(worker, key=key, day=day, bucket="public", traffic_bytes=b.GIB)
                return "reserved"
            except ValidationError:
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, ("parallel1", "parallel2")))
    assert results == ["denied", "reserved"]
    with session_scope(settings) as verify:
        assert verify.query(FreeAllocation).filter_by(day=day, bucket="public").count() == 10
