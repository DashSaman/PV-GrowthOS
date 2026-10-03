"""Shared test fixtures.

TEST_DB=postgres (default in CI matrix) runs against the CI service;
locally defaults to a temp SQLite file so `pytest` works anywhere.
Schema is created by running Alembic migrations — the same path production
uses, which doubles as a migration test.
"""

from __future__ import annotations

import os
import tempfile

import pytest


def _database_url() -> str:
    if os.environ.get("TEST_DB") == "postgres":
        return os.environ.get(
            "PVG_DATABASE_URL",
            "postgresql+psycopg://pv_growth:pv_growth@localhost:5432/pv_growth_test",
        )
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return f"sqlite+pysqlite:///{path}"


@pytest.fixture(scope="session")
def database_url() -> str:
    return _database_url()


@pytest.fixture(scope="session")
def settings(database_url: str):
    os.environ["PVG_DATABASE_URL"] = database_url
    os.environ.setdefault("PVG_ADMIN_TOKEN", "test-admin-token")
    os.environ.setdefault("PVG_SCHEDULER_ENABLED", "0")

    from pv_growth.core.config import get_settings

    # bust the lru_cache so the env we just set is picked up
    get_settings.cache_clear()
    s = get_settings()
    yield s
    get_settings.cache_clear()
    from pv_growth.database.base import dispose_all

    dispose_all()
    if database_url.startswith("sqlite"):
        try:
            os.remove(database_url.split("///")[-1])
        except OSError:
            pass


@pytest.fixture(scope="session", autouse=True)
def _migrate(settings):
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
def session(settings):
    from pv_growth.database.base import session_scope

    with session_scope(settings) as sess:
        yield sess


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from pv_growth.main import create_app

    with TestClient(create_app()) as c:
        yield c
