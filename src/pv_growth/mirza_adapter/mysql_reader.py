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

    def fetch_invoices_after(self, invoice_id: int, limit: int = 500) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id_invoice, id_user, username, name_product, price_product, "
                "Volume, Service_time, Status, refral "
                "FROM invoice WHERE id_invoice > %s ORDER BY id_invoice LIMIT %s",
                (invoice_id, limit),
            )
            return list(cur.fetchall())

    def max_invoice_id(self) -> int:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id_invoice), 0) AS m FROM invoice")
            return int(cur.fetchone()["m"])


def _watermark(session: Session) -> int:
    row = session.get(AppConfig, WATERMARK_KEY)
    if row is None:
        return 0
    return int((row.value or {}).get("last_invoice_id", 0))


def _advance_watermark(session: Session, invoice_id: int) -> None:
    row = session.get(AppConfig, WATERMARK_KEY)
    if row is None:
        session.add(AppConfig(key=WATERMARK_KEY, value={"last_invoice_id": invoice_id}))
    else:
        row.value = {"last_invoice_id": invoice_id}
        row.updated_at = utcnow()


def sync_mirza(session: Session, settings: Settings,
               reader: MirzaMySQLReader | None = None,
               *, limit: int = 100) -> dict:
    """Pull new Mirza invoices and ingest them as events — idempotent by
    invoice id. Passive: emits events only; rewards/messages stay flag-gated."""
    reader = reader or MirzaMySQLReader(settings)
    stats = {"fetched": 0, "payments": 0, "checkouts": 0, "skipped": 0}

    first_run = session.get(AppConfig, WATERMARK_KEY) is None
    try:
        if first_run:
            # forward-only: start from the tip, never replay historical invoices
            # (a deliberate backfill can lower the watermark manually)
            tip = reader.max_invoice_id()
            _advance_watermark(session, tip)
            log.info("mirza watermark initialized to tip", last_invoice_id=tip)
            return {**stats, "initialized": True, "last_invoice_id": tip}
        last_id = _watermark(session)
        invoices = reader.fetch_invoices_after(last_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 — Mirza outage must never hurt GrowthOS
        log.warning("mirza mysql read failed (isolated)", error=str(exc))
        return {**stats, "error": str(exc)[:120]}

    for invoice in invoices:
        stats["fetched"] += 1
        inv_id = int(invoice["id_invoice"])
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

        _advance_watermark(session, inv_id)  # advance even on skips

    if stats["fetched"]:
        log.info("mirza sync", **stats)
    return stats


class FakeMirzaMySQL:
    """In-memory invoice source for tests."""

    def __init__(self, invoices: list[dict]) -> None:
        self.invoices = invoices

    def fetch_invoices_after(self, invoice_id: int, limit: int = 100) -> list[dict]:
        return [i for i in self.invoices if int(i["id_invoice"]) > invoice_id][:limit]
