"""Migration regressions for production-hardening schema changes."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _alembic(repo: Path, database_url: str, *args: str) -> None:
    from pv_growth.core.config import get_settings

    prior_url = os.environ.get("PVG_DATABASE_URL")
    os.environ["PVG_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "alembic"))
    try:
        operation, revision = args
        getattr(command, operation)(config, revision)
    finally:
        if prior_url is None:
            os.environ.pop("PVG_DATABASE_URL", None)
        else:
            os.environ["PVG_DATABASE_URL"] = prior_url
        get_settings.cache_clear()


def test_0009_backfills_only_proven_commission_and_round_trips(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "migration.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    _alembic(repo, database_url, "upgrade", "0008")

    now = "2026-10-06 00:00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO partners "
            "(id, code, kind, name, status, clicks, starts, trials, orders, meta, created_at) "
            "VALUES (1, 'ptlegacy', 'affiliate', 'Legacy', 'active', 0, 0, 0, 2, '{}', ?)",
            (now,),
        )
        conn.executemany(
            "INSERT INTO commission_entries "
            "(id, partner_id, order_ref, amount_cents, status, dedupe_key, created_at) "
            "VALUES (?, 1, ?, ?, 'approved', ?, ?)",
            [
                (1, "order-proven", 100000, "commission:1:order-proven", now),
                (2, "order-unknown", 50000, "commission:1:order-unknown", now),
            ],
        )
        conn.execute(
            "INSERT INTO events "
            "(event_id, event_type, occurred_at, ingested_at, idempotency_key, metadata) "
            "VALUES (?, 'PARTNER_CONVERSION', ?, ?, ?, ?)",
            (
                "evt-migration-proven",
                now,
                now,
                "pconv:commission:1:order-proven",
                json.dumps(
                    {
                        "partner_id": 1,
                        "order_ref": "order-proven",
                        "amount_cents": 100000,
                        "commission_cents": 15000,
                    }
                ),
            ),
        )

    _alembic(repo, database_url, "upgrade", "0009")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(commission_entries)")}
        rows = conn.execute(
            "SELECT order_ref, amount_cents, commission_cents FROM commission_entries ORDER BY id"
        ).fetchall()
    assert "commission_cents" in columns
    assert rows == [
        ("order-proven", 100000, 15000),
        ("order-unknown", 50000, None),
    ]

    _alembic(repo, database_url, "downgrade", "0008")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(commission_entries)")}
    assert "commission_cents" not in columns

    _alembic(repo, database_url, "upgrade", "0009")
    with sqlite3.connect(db_path) as conn:
        value = conn.execute("SELECT commission_cents FROM commission_entries WHERE id = 1").fetchone()[0]
    assert value == 15000


def test_0010_provisioning_retry_schema_round_trips(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "provisioning-migration.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    _alembic(repo, database_url, "upgrade", "0009")

    _alembic(repo, database_url, "upgrade", "0010")


def test_0011_lifecycle_delivery_schema_round_trips(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "lifecycle-migration.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    _alembic(repo, database_url, "upgrade", "0010")

    _alembic(repo, database_url, "upgrade", "0011")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(message_log)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(message_log)")}
    assert {"attempts", "send_ordinal"} <= columns
    assert "ix_message_log_delivery_slot" in indexes

    _alembic(repo, database_url, "downgrade", "0010")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(message_log)")}
    assert "attempts" not in columns
    assert "send_ordinal" not in columns

    _alembic(repo, database_url, "upgrade", "0011")
    with sqlite3.connect(db_path) as conn:
        claim_columns = {row[1] for row in conn.execute("PRAGMA table_info(exclusive_claims)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"provision_attempts", "last_provision_error"} <= claim_columns
    assert "provisioning_quota_locks" in tables

    _alembic(repo, database_url, "downgrade", "0009")
    with sqlite3.connect(db_path) as conn:
        claim_columns = {row[1] for row in conn.execute("PRAGMA table_info(exclusive_claims)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "provision_attempts" not in claim_columns
    assert "last_provision_error" not in claim_columns
    assert "provisioning_quota_locks" not in tables

    _alembic(repo, database_url, "upgrade", "0010")
