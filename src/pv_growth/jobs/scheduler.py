"""Background scheduler: enqueues periodic scans and drains the job queue.

Started by the app lifespan when PVG_SCHEDULER_ENABLED=1. Idempotent by
design — periodic enqueues carry dedupe keys, and the runner's atomic claim
guarantees single execution even with overlapping ticks."""

from __future__ import annotations

import threading
import time
from datetime import datetime

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger

log = get_logger("scheduler")


def enqueue_free_growth(session, settings: Settings, now: datetime | None = None) -> None:
    from pv_growth.database.types import utcnow
    from pv_growth.free_config.shared_public import SHARED_HOURS_UTC, growth_policy, local_day
    from pv_growth.jobs import service as jobs

    now = now or utcnow()
    day = local_day(now).isoformat()
    policy = growth_policy(session)
    from pv_growth.acquisition.daily import enabled_policy

    if enabled_policy(session) is not None and now.hour == 6 and 15 <= now.minute < 30:
        jobs.enqueue(
            session,
            "acquisition.daily_invite",
            {"day": day},
            idempotency_key=f"acquisition_daily:{day}",
            max_attempts=1,
        )
    if policy.get("public_shared_enabled") and now.hour in SHARED_HOURS_UTC:
        slot = SHARED_HOURS_UTC.index(now.hour) + 1
        jobs.enqueue(
            session,
            "free_config.shared_publish",
            {"day": day, "slot": slot},
            idempotency_key=f"shared_publish:{day}:{slot}",
            max_attempts=3,
        )
    if policy.get("lottery_enabled") and now.hour == 19 and now.minute >= 30:
        jobs.enqueue(
            session,
            "free_config.lottery_draw",
            {"day": day, "campaign_code": "pv_daily_lottery"},
            idempotency_key=f"lottery_draw:{day}",
            max_attempts=3,
        )


class Scheduler:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None

    def _loop(self) -> None:
        from pv_growth.jobs.runner import run_tick

        settings = self._settings
        log.info("scheduler loop started", interval=settings.scheduler_interval_seconds)

        while not self._stop.is_set():
            try:
                self._enqueue_periodic()
                run_tick(settings, worker_id="scheduler")
            except Exception as exc:  # noqa: BLE001 — the loop itself must never die
                log.error("scheduler tick failed", error=str(exc))
            self._stop.wait(settings.scheduler_interval_seconds)
        log.info("scheduler loop stopped")

    def _poll_loop(self) -> None:
        from pv_growth.api.webhooks import process_update
        from pv_growth.telegram.poller import TelegramPoller

        poller = TelegramPoller(self._settings, process_update)
        log.info("telegram long-polling worker started")
        while not self._stop.is_set():
            try:
                result = poller.poll_once()
            except Exception as exc:  # noqa: BLE001 — polling cannot kill scheduling
                log.error("telegram polling worker failed", error=str(exc))
                result = -1
            if result <= 0:
                self._stop.wait(1.0)
        log.info("telegram long-polling worker stopped")

    def _enqueue_periodic(self) -> None:
        from pv_growth.database.base import session_scope
        from pv_growth.jobs import service as jobs

        with session_scope(self._settings) as session:
            enqueue_free_growth(session, self._settings)
            # lifecycle scan every interval; dedupe key collapses overlapping ticks
            bucket = int(time.time() // self._settings.scheduler_interval_seconds)
            jobs.enqueue(session, "lifecycle.scan", {}, idempotency_key=f"lifecycle_scan:{bucket}")
            jobs.enqueue(session, "content.publish_due", {}, idempotency_key=f"content_publish:{bucket}")
            # Planning hourly is bounded; day/slot content dedupe is a second guard.
            plan_bucket = int(time.time() // 3600)
            jobs.enqueue(session, "content.plan", {}, idempotency_key=f"content_plan:{plan_bucket}")
            insights_bucket = int(time.time() // 900)
            jobs.enqueue(
                session,
                "instagram.insights_sync",
                {},
                idempotency_key=f"instagram_insights:{insights_bucket}",
            )
            maintenance_bucket = int(time.time() // 86400)
            jobs.enqueue(
                session,
                "maintenance.retention",
                {},
                idempotency_key=f"maintenance_retention:{maintenance_bucket}",
            )
            # Mirza read-only sync every ~5 minutes (only when configured)
            if self._settings.mirza_mysql_host:
                mbucket = int(time.time() // 300)
                jobs.enqueue(session, "mirza.sync", {}, idempotency_key=f"mirza_sync:{mbucket}")
            # free-service expiry sweep (only when provisioning is configured)
            if self._settings.provisioning_base_url and self._settings.provisioning_token:
                sbucket = int(time.time() // 300)
                jobs.enqueue(session, "provisioning.sweep", {}, idempotency_key=f"prov_sweep:{sbucket}")
            # Public free-config acquisition loop: refresh approved sources at
            # a bounded cadence, and publish at most one post per configured
            # UTC hour. Effect-level post dedupe is a second guard.
            collect_seconds = max(1, self._settings.free_collect_interval_hours) * 3600
            cbucket = int(time.time() // collect_seconds)
            jobs.enqueue(
                session,
                "free_config.collect",
                {},
                idempotency_key=f"free_collect:{cbucket}",
            )
            publish_hours: list[int] = []
            for raw_hour in str(self._settings.free_publish_hours_utc).split(","):
                try:
                    hour = int(raw_hour.strip())
                except ValueError:
                    continue
                if 0 <= hour <= 23 and hour not in publish_hours:
                    publish_hours.append(hour)
            publish_hours = publish_hours[:2]  # hard safety cap: never more than two channel slots/day
            now_utc = time.gmtime(time.time())
            if now_utc.tm_hour in publish_hours:
                slot = publish_hours.index(now_utc.tm_hour) + 1
                day_key = time.strftime("%Y-%m-%d", now_utc)
                jobs.enqueue(
                    session,
                    "free_config.publish",
                    {"slot": slot},
                    idempotency_key=f"free_publish:{day_key}:{slot}",
                )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="pv-growth-scheduler", daemon=True)
        self._thread.start()
        if (
            self._settings.scheduler_enabled
            and self._settings.telegram_polling_enabled
            and self._settings.telegram_bot_token
        ):
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="pv-growth-telegram-poller",
                daemon=True,
            )
            self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=35)
            self._poll_thread = None
