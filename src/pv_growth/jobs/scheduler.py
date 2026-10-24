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
    from pv_growth.free_config.autonomous import enqueue_native, hours_for

    enqueue_native(session, settings, now)
    shared_hours = (
        hours_for(session, local_day(now)) if policy.get("server_autonomy_enabled") else SHARED_HOURS_UTC
    )
    from pv_growth.acquisition.intro import policy as intro_policy
    from pv_growth.database.models import AppConfig

    cohort = session.get(AppConfig, "pv_intro_cohort_20261013")
    intro = intro_policy(session, settings)
    if intro and day == intro[0]["planning_day"] and cohort and cohort.value.get("status") == "running":
        jobs.enqueue(
            session,
            "acquisition.intro_plan",
            {},
            idempotency_key=f"intro_plan_48h:{now.isoformat()[:16]}",
            max_attempts=1,
            priority=-5,
        )
    from pv_growth.acquisition.daily import enabled_policy

    if enabled_policy(session) is not None and now.hour == 6 and 15 <= now.minute < 30:
        jobs.enqueue(
            session,
            "acquisition.daily_invite",
            {"day": day},
            idempotency_key=f"acquisition_daily:{day}",
            max_attempts=1,
        )
    # A UTC hour can span Tehran midnight. Keep a slot on its scheduled
    # local day; after rollover, skip it rather than replaying yesterday or
    # consuming tomorrow's slot 9 before its actual 23:30 schedule.
    shared_day = local_day(now.replace(minute=0, second=0, microsecond=0)).isoformat()
    if policy.get("public_shared_enabled") and now.hour in shared_hours and shared_day == day:
        slot = shared_hours.index(now.hour) + 1
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
                executed = run_tick(settings, worker_id="scheduler")
                if executed >= settings.job_batch_size and self._intro_backlog():
                    continue
            except Exception as exc:  # noqa: BLE001 — the loop itself must never die
                log.error("scheduler tick failed", error=str(exc))
            self._stop.wait(settings.scheduler_interval_seconds)
        log.info("scheduler loop stopped")

    def _intro_backlog(self) -> bool:
        from pv_growth.acquisition.intro import continuous_backlog
        from pv_growth.database.base import session_scope

        with session_scope(self._settings) as session:
            return continuous_backlog(session, self._settings)

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
            from pv_growth.database.types import utcnow
            from pv_growth.growth_research import enqueue_daily

            enqueue_daily(session, utcnow())
            from pv_growth.acquisition.purchase_journey import enqueue_monitor

            enqueue_monitor(session, utcnow())
            from pv_growth.acquisition.post_report import enqueue_monitor as enqueue_post_monitor

            enqueue_post_monitor(session, utcnow())
            from pv_growth.acquisition.outreach_report import enqueue_monitor as enqueue_outreach_monitor

            enqueue_outreach_monitor(session, utcnow())
            from pv_growth.acquisition.maps_report import enqueue_monitor as enqueue_maps_monitor

            enqueue_maps_monitor(session, utcnow())
            from pv_growth.acquisition.mail_jobs import enqueue as enqueue_mail_replies

            enqueue_mail_replies(session, utcnow())
            from pv_growth.acquisition.maps_mail_jobs import enqueue as enqueue_maps_mail_replies

            enqueue_maps_mail_replies(session, utcnow())
            from pv_growth.acquisition.needs_mail_jobs import enqueue as enqueue_needs_mail_replies
            from pv_growth.acquisition.needs_report import enqueue_monitor as enqueue_needs_monitor

            enqueue_needs_monitor(session, utcnow())
            enqueue_needs_mail_replies(session, utcnow())
            from pv_growth.acquisition.expansion_mail_jobs import enqueue as enqueue_expansion_mail_replies
            from pv_growth.acquisition.expansion_report import enqueue_monitor as enqueue_expansion_monitor

            enqueue_expansion_monitor(session, utcnow())
            enqueue_expansion_mail_replies(session, utcnow())
            from pv_growth.core.flags import FlagService

            if FlagService(self._settings).enabled("COMPETITOR_WATCH_ENABLED"):
                watch_bucket = int(time.time() // (6 * 3600))
                jobs.enqueue(
                    session,
                    "competitors.watch",
                    {},
                    idempotency_key=f"competitor_watch:{watch_bucket}",
                    max_attempts=1,
                )
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
            from pv_growth.free_config.shared_public import growth_policy

            autonomous = growth_policy(session).get("server_autonomy_enabled") is True
            if autonomous:
                collect_seconds = 3600  # refresh only approved sources; bounded fetchers still apply
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
            from pv_growth.free_config.shared_public import local_day
            from pv_growth.free_config.web_publication import WEB_HOURS_UTC, direct_enabled

            direct = direct_enabled(session)
            publish_hours = list(WEB_HOURS_UTC) if direct else publish_hours[:2]
            now_utc = time.gmtime(time.time())
            from datetime import UTC

            stamp = datetime.fromtimestamp(time.time(), UTC).replace(tzinfo=None)
            same_scheduled_day = local_day(stamp) == local_day(
                stamp.replace(minute=0, second=0, microsecond=0)
            )
            if not autonomous and now_utc.tm_hour in publish_hours and (not direct or same_scheduled_day):
                slot = publish_hours.index(now_utc.tm_hour) + 1
                day_key = local_day(stamp).isoformat() if direct else time.strftime("%Y-%m-%d", now_utc)
                jobs.enqueue(
                    session,
                    "free_config.publish",
                    {"slot": slot, "day": day_key, "direct": direct},
                    idempotency_key=f"{'free_direct' if direct else 'free_publish'}:{day_key}:{slot}",
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
        worker_stopped = True
        if self._thread is not None:
            self._thread.join(timeout=10)
            worker_stopped = not self._thread.is_alive()
            self._thread = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=35)
            self._poll_thread = None
        from pv_growth.acquisition.intro import close_worker_resources

        if worker_stopped:
            close_worker_resources()
