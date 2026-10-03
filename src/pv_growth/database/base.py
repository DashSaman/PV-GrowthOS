"""Database engine/session management.

Production uses the existing PostgreSQL 14 server (database ``pv_growth``).
Tests use SQLite; every column type used in models degrades gracefully to
SQLite via variants (see database/types.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pv_growth.core.config import Settings

_engines: dict[str, Engine] = {}
_session_factories: dict[str, sessionmaker] = {}


def get_engine(settings: Settings) -> Engine:
    url = settings.database_url
    if url not in _engines:
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
        _engines[url] = create_engine(url, **kwargs)
    return _engines[url]


def get_session_factory(settings: Settings) -> sessionmaker:
    url = settings.database_url
    if url not in _session_factories:
        _session_factories[url] = sessionmaker(
            bind=get_engine(settings), expire_on_commit=False, autoflush=False
        )
    return _session_factories[url]


@contextmanager
def session_scope(settings: Settings) -> Iterator[Session]:
    """Transactional session scope; commits on success, rolls back on error."""
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_all() -> None:
    """Close all cached engines (used by tests and app shutdown)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    _session_factories.clear()
