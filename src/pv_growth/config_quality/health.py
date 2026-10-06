"""Limited health checking — a handful of TCP connects, never mass speed tests.

Hard caps come from settings (health_check_max default 5, concurrency 3).
This protects the production VPN server (spec §13).
"""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

from pv_growth.core.config import Settings
from pv_growth.core.logging import get_logger

log = get_logger("health")


def tcp_check(host: str, port: int, timeout: float) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        return False
    # Public feeds are untrusted input. Resolve first and fail closed if DNS
    # points at any non-public address, then connect to the vetted IP itself
    # so a second DNS lookup cannot rebind into localhost/private networks.
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            return False
    except ValueError:
        return False
    for address in addresses:
        try:
            with socket.create_connection((address, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def check_top_candidates(candidates: list[tuple[int, str, int]], settings: Settings) -> dict[int, bool]:
    """candidates: [(config_db_id, host, port)] already capped by the caller.
    Returns {config_db_id: reachable}."""
    if not candidates:
        return {}
    capped = candidates[: settings.health_check_max]
    results: dict[int, bool] = {}
    with ThreadPoolExecutor(max_workers=settings.health_check_concurrency) as pool:
        futures = {
            pool.submit(tcp_check, host, port, settings.health_check_timeout_seconds): cid
            for cid, host, port in capped
        }
        for future, cid in futures.items():
            results[cid] = future.result()
    log.info("health check done", checked=len(capped))
    return results
