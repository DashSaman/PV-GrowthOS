"""Read-only Mirza MySQL adapter (the REAL integration path).

Mirza is the source of truth; this reader only SELECTs from its existing
`hajsaman` database (invoice + user tables) through a dedicated read-only
MySQL account. Nothing in Mirza is ever modified.

Mapping (verified against the installed schema 2026-10-03):
  invoice.Status = 'active'      -> PAYMENT_SUCCESS + SERVICE_CREATED
  invoice.Status = 'unpaid'      -> CHECKOUT_STARTED
  invoice.id_user                -> GrowthOS telegram_user_id (Mirza user.id
                                    holds Telegram ids — 9/10-digit range)
  invoice.id_invoice             -> idempotency watermark + order ref
  invoice.refral                 -> referral linkage (REFERRAL flag gates rewards)

Connection settings mirror the production topology: MySQL is reachable via
the GrowthOS-owned socat forwarder on 172.23.77.1:3306.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger
from pv_growth.database.models import AppConfig
from pv_growth.database.types import utcnow
from pv_growth.events.service import get_or_create_user, ingest

log = get_logger("mirza.mysql")

WATERMARK_KEY = "mirza_sync_state"
_PAID = "active"

_NUMBER_RE = re.compile(r"\d+")


def _to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    digits = _NUMBER_RE.findall(str(value).replace(",", ""))
    return int(digits[0]) if digits else None


class MirzaMySQLReader:
    """Thin read-only client. `connect` returns a DB-API connection; kept
    injectable for tests (FakeMirzaMySQL below)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def connect(self):
        import pymysql

        return pymysql.connect(
            host=self._settings.mirza_mysql_host,
            port=self._settings.mirza_mysql_port,
            user=self._settings.mirza_mysql_user,
            password=self._settings.mirza_mysql_password,
            database="hajsaman",
            charset="utf8mb4",
            connect_timeout=8,
            read_timeout=15,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def list_invoice_ids(self) -> list[str]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id_invoice FROM invoice")
            return [str(r["id_invoice"]) for r in cur.fetchall()]

    def fetch_by_ids(self, ids: list[str]) -> list[dict]:
        rows: list[dict] = []
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            placeholders = ",".join(["%s"] * len(chunk))
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(  # noqa: S608 - placeholders are %s-bound, not interpolated
                    "SELECT id_invoice, id_user, username, name_product, price_product, "
                    "Volume, Service_time, Status, refral "
                    f"FROM invoice WHERE id_invoice IN ({placeholders})",
                    tuple(chunk),
                )
                rows.extend(cur.fetchall())
        return rows


def _load_seen(session: Session) -> set[str]:
    row = session.get(AppConfig, WATERMARK_KEY)
    return set((row.value or {}).get("seen", [])) if row else set()


def _save_seen(session: Session, seen: set[str]) -> None:
    row = session.get(AppConfig, WATERMARK_KEY)
    if row is None:
        row = AppConfig(key=WATERMARK_KEY, value={})
        session.add(row)
        session.flush()  # visible to later gets within the same transaction
    row.value = {"seen": sorted(seen), "count": len(seen)}
    row.updated_at = utcnow()


def sync_mirza(session: Session, settings: Settings,
               reader: MirzaMySQLReader | None = None,
               *, limit: int = 300) -> dict:
    """Poll Mirza invoices (read-only) and ingest new/changed ones as events.

    Mirza's invoice PK is a random hex string with no monotonic order and no
    reliable timestamp column, so change detection uses a seen-id set plus
    event-level idempotency keys (mirza:pay:<id> / mirza:checkout:<id>) —
    re-scans are always safe, and unpaid->active transitions emit
    PAYMENT_SUCCESS after the earlier CHECKOUT_STARTED. Passive: events only;
    rewards/messages stay flag-gated."""
    reader = reader or MirzaMySQLReader(settings)
    stats = {"scanned": 0, "new": 0, "payments": 0, "checkouts": 0, "skipped": 0}

    try:
        all_ids = reader.list_invoice_ids()
        seen = _load_seen(session)
        fresh = [i for i in all_ids if i not in seen][:limit]
        stats["scanned"] = len(all_ids)
        if not fresh:
            return stats
        rows = reader.fetch_by_ids(fresh)
    except Exception as exc:  # noqa: BLE001 — Mirza outage must never hurt GrowthOS
        log.warning("mirza mysql read failed (isolated)", error=str(exc))
        return {**stats, "error": str(exc)[:120]}

    for invoice in rows:
        stats["new"] += 1
        inv_id = str(invoice["id_invoice"])
        tg_user = _to_int(invoice.get("id_user"))
        status = str(invoice.get("Status") or "").strip().lower()

        if tg_user is None:
            stats["skipped"] += 1
        else:
            user, _ = get_or_create_user(session, telegram_user_id=tg_user,
                                         username=str(invoice.get("username") or None))
            amount = _to_int(invoice.get("price_product"))
            meta = {
                "mirza_invoice_id": inv_id,
                "product": invoice.get("name_product"),
                "amount": amount,
                "volume": invoice.get("Volume"),
                "service_time": invoice.get("Service_time"),
                "source": "mirza_db",
            }
            if status == _PAID:
                ingest(session, "PAYMENT_SUCCESS", user_id=user.id,
                       idempotency_key=f"mirza:pay:{inv_id}",
                       metadata={**meta, "amount_cents": (amount or 0) * 10})
                ingest(session, "SERVICE_CREATED", user_id=user.id,
                       idempotency_key=f"mirza:svc:{inv_id}", metadata=meta)
                stats["payments"] += 1
            elif status == "unpaid":
                ingest(session, "CHECKOUT_STARTED", user_id=user.id,
                       idempotency_key=f"mirza:checkout:{inv_id}", metadata=meta)
                stats["checkouts"] += 1
            else:
                stats["skipped"] += 1
        seen.add(inv_id)

    _save_seen(session, seen)
    log.info("mirza sync", **stats)
    return stats


class FakeMirzaMySQL:
    """In-memory invoice source for tests."""

    def __init__(self, invoices: list[dict]) -> None:
        self.invoices = invoices

    def list_invoice_ids(self) -> list[str]:
        return [str(i["id_invoice"]) for i in self.invoices]

    def fetch_by_ids(self, ids: list[str]) -> list[dict]:
        wanted = set(ids)
        return [i for i in self.invoices if str(i["id_invoice"]) in wanted]
