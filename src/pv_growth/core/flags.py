"""Feature flags: env-configured defaults, DB overrides, short-TTL cache.

Every customer-facing or dangerous automation starts disabled. An env
``PVG_FLAG_X_ENABLED=1`` supplies the environment default. A DB override set
through admin wins in either direction so the DB can always act as a kill switch.
"""

from __future__ import annotations

import time

from sqlalchemy import text

from pv_growth.core.config import Settings
from pv_growth.database.base import get_engine

FLAG_KEYS = (
    "ATTRIBUTION_ENABLED",
    "FREE_CONFIG_ENABLED",
    "PUBLIC_CONFIG_ENABLED",
    "PV_EXCLUSIVE_CONFIG_ENABLED",
    "LIFECYCLE_AUTOMATION_ENABLED",
    "REFERRAL_ENABLED",
    "PARTNER_ENABLED",
    "CONTENT_ENGINE_ENABLED",
    "INSTAGRAM_AUTOMATION_ENABLED",
    "COMPETITOR_WATCH_ENABLED",
    "EXPERIMENTS_ENABLED",
)

_TTL_SECONDS = 15.0


class FlagService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[str, bool] = {}
        self._loaded_at = 0.0

    @staticmethod
    def _env_name(key: str) -> str:
        return "flag_" + key.lower()

    def _default(self, key: str) -> bool:
        return bool(getattr(self._settings, self._env_name(key), False))

    def _load_overrides(self) -> dict[str, bool]:
        try:
            with get_engine(self._settings).connect() as conn:
                rows = conn.execute(text("SELECT key, enabled FROM feature_flag")).fetchall()
            return {str(k): bool(v) for k, v in rows if k in FLAG_KEYS}
        except Exception:
            # table missing (pre-migration) or DB down: fall back to defaults
            return {}

    def _refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._cache and now - self._loaded_at < _TTL_SECONDS:
            return
        overrides = self._load_overrides()
        self._cache = {key: overrides.get(key, self._default(key)) for key in FLAG_KEYS}
        self._loaded_at = now

    def enabled(self, key: str) -> bool:
        if key not in FLAG_KEYS:
            raise KeyError(f"unknown feature flag: {key}")
        self._refresh()
        return self._cache[key]

    def snapshot(self) -> dict[str, bool]:
        self._refresh(force=True)
        return dict(self._cache)
