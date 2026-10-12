"""Bounded public competitor observations; never sends messages or changes offers."""

from pv_growth.competitors.watcher import run_watch
from pv_growth.core.flags import FlagService
from pv_growth.jobs.runner import handler


@handler("competitors.watch")
def watch_job(session, settings, payload):
    run_watch(session, settings, FlagService(settings))
