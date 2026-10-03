"""Background scheduler: enqueues periodic scans and drains the job queue.

Started by the app lifespan when PVG_SCHEDULER_ENABLED=1. Idempotent by
design — periodic enqueues carry dedupe keys, and the runner's atomic claim
guarantees single execution even with overlapping ticks."""

from __future__ import annotations

import threading
import time

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger

log = get_logger("scheduler")


class Scheduler:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        from pv_growth.jobs.runner import run_tick

        settings = self._settings
        log.info("scheduler loop started", interval=settings.scheduler_interval_seconds)
        poller = None
        if (settings.scheduler_enabled and settings.telegram_polling_enabled
                and settings.telegram_bot_token):
            from pv_growth.api.webhooks import process_update
            from pv_growth.telegram.poller import TelegramPoller

            poller = TelegramPoller(settings, lambda u: process_update(u))
            log.info("telegram long-polling enabled")

        while not self._stop.is_set():
            try:
                self._enqueue_periodic()
                run_tick(settings, worker_id="scheduler")
                if poller is not None:
                    poller.poll_once()  # blocks up to 25s; fine in this thread
            except Exception as exc:  # noqa: BLE001 — the loop itself must never die
                log.error("scheduler tick failed", error=str(exc))
            self._stop.wait(settings.scheduler_interval_seconds)
        log.info("scheduler loop stopped")

    def _enqueue_periodic(self) -> None:
        from pv_growth.database.base import session_scope
        from pv_growth.jobs import service as jobs

        with session_scope(self._settings) as session:
            # lifecycle scan every interval; dedupe key collapses overlapping ticks
            bucket = int(time.time() // self._settings.scheduler_interval_seconds)
            jobs.enqueue(session, "lifecycle.scan", {},
                         idempotency_key=f"lifecycle_scan:{bucket}")
            # Mirza read-only sync every ~5 minutes (only when configured)
            if self._settings.mirza_mysql_host:
                mbucket = int(time.time() // 300)
                jobs.enqueue(session, "mirza.sync", {},
                             idempotency_key=f"mirza_sync:{mbucket}")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="pv-growth-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
