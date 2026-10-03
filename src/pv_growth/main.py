"""FastAPI application factory for PV GrowthOS."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from pv_growth import __version__
from pv_growth.core.config import get_settings
from pv_growth.core.logging import configure_logging, get_logger

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("startup", env=settings.env, version=__version__)
    scheduler = None
    if settings.scheduler_enabled:
        from pv_growth.jobs.scheduler import Scheduler

        scheduler = Scheduler(get_settings())
        scheduler.start()
    yield
    if scheduler is not None:
        scheduler.stop()
    from pv_growth.database.base import dispose_all

    dispose_all()
    log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="PV GrowthOS", version=__version__, lifespan=lifespan,
                  docs_url=None if settings.env == "production" else "/docs")

    from pv_growth.api import admin, events, health, webhooks

    app.include_router(health.router)
    app.include_router(events.router, prefix="/api")
    app.include_router(admin.router, prefix="/admin/api")
    app.include_router(webhooks.router, prefix="/webhooks")
    return app


app = create_app()
