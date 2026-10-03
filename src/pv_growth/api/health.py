"""Health and readiness endpoints.

``/health`` is process liveness (no dependencies). ``/ready`` verifies the
database is reachable — it must return 503 when the DB is down so a broken
deployment rolls back instead of serving traffic.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from pv_growth import __version__
from pv_growth.core.config import get_settings
from pv_growth.database.base import get_engine

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "app": "pv-growth", "version": __version__}


@router.get("/ready")
def ready(response: Response) -> dict:
    settings = get_settings()
    try:
        with get_engine(settings).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready", "reason": f"database: {type(exc).__name__}"}
    return {"status": "ready", "env": settings.env}
