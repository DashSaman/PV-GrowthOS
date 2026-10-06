"""Protected-service smoke tests — run before AND after every GrowthOS deploy.

Read-only HTTP/TCP checks against existing production services. If any check
fails AFTER a GrowthOS action, the deployment contract requires immediate
GrowthOS rollback (never "fixing" the protected service).
"""

from __future__ import annotations

import socket
import sys

import httpx

from pv_growth.core.config import get_settings


def check_http(name: str, url: str, timeout: float) -> tuple[str, bool, str]:
    try:
        # internal hosts (x-ui etc.) use self-signed certs; smoke only checks liveness
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            verify=False,  # noqa: S501
        )
        ok = resp.status_code < 500  # protected services may 401/404; only 5xx/down is failure
        return name, ok, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return name, False, f"{type(exc).__name__}: {exc}"[:120]


def check_tcp(name: str, host: str, port: int, timeout: float) -> tuple[str, bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return name, True, "tcp connected"
    except OSError as exc:
        return name, False, f"{type(exc).__name__}: {exc}"[:120]


def run(args: list[str] | None = None) -> int:
    settings = get_settings()
    timeout = settings.smoke_timeout_seconds
    results = [
        check_http("mirza", settings.smoke_mirza_url, timeout),
        check_http("pv_reseller", settings.smoke_reseller_url, timeout),
        check_http("akh_bot", settings.smoke_akh_url, timeout),
        check_http("apache", settings.smoke_apache_url, timeout),
        check_http("xui", settings.smoke_xui_url, timeout),
        check_tcp("postgresql", settings.smoke_pg_host, settings.smoke_pg_port, timeout),
        check_http("growthos", settings.smoke_growth_url, timeout),
    ]
    print("== PV GrowthOS protected-service smoke ==")
    failed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        failed += not ok
        print(f"[{mark}] {name}: {detail}")
    if failed:
        msg = "SMOKE FAILED (%d services unhealthy) - after a GrowthOS action: ROLLBACK GrowthOS"
        print(msg % failed)
        return 1
    print(f"SMOKE OK ({len(results)} services healthy)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
