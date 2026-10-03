"""Shared API dependencies: admin-token auth + in-process rate limiting."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict

from fastapi import Header, HTTPException, Request, status

from pv_growth.core.config import get_settings


def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Constant-time token check; when no token is configured, always refuse."""
    settings = get_settings()
    if not settings.admin_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin disabled (no token configured)")
    if not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


class TokenBucket:
    """Small in-process limiter (per key). Enough for a single-container v1."""

    def __init__(self, rate_per_minute: int = 120, burst: int = 30) -> None:
        self.rate = rate_per_minute / 60.0
        self.burst = burst
        self._tokens: dict[str, float] = defaultdict(lambda: float(burst))
        self._last: dict[str, float] = defaultdict(time.monotonic)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        elapsed = now - self._last[key]
        self._last[key] = now
        self._tokens[key] = min(self.burst, self._tokens[key] + elapsed * self.rate)
        if self._tokens[key] >= 1.0:
            self._tokens[key] -= 1.0
            return True
        return False


_event_bucket = TokenBucket(rate_per_minute=300, burst=60)
_webhook_bucket = TokenBucket(rate_per_minute=600, burst=120)


def rate_limit_events(request: Request) -> None:
    key = request.client.host if request.client else "anon"
    if not _event_bucket.allow(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")


def rate_limit_webhook(request: Request) -> None:
    key = request.client.host if request.client else "anon"
    if not _webhook_bucket.allow(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
