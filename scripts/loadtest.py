"""Lightweight load probe for GrowthOS (NOT a stress tool).

Hits /health, /ready and the events API with a small concurrent burst to
verify latency under modest load. Run from the host against 127.0.0.1:8350.
Deliberately modest: this shares a production VPN server.
"""

from __future__ import annotations

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def probe(base: str, path: str, n: int, concurrency: int, headers: dict | None = None) -> dict:
    latencies: list[float] = []
    failures = 0

    def hit(_: int) -> None:
        nonlocal failures
        start = time.perf_counter()
        try:
            resp = httpx.get(base + path, headers=headers or {}, timeout=10.0)
            if resp.status_code >= 500:
                failures += 1
        except httpx.HTTPError:
            failures += 1
        latencies.append((time.perf_counter() - start) * 1000.0)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(hit, range(n)))

    return {
        "path": path, "requests": n, "failures": failures,
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "max_ms": round(max(latencies), 1) if latencies else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8350")
    parser.add_argument("--n", type=int, default=50, help="requests per endpoint")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()

    results = [probe(args.base, "/health", args.n, args.concurrency),
               probe(args.base, "/ready", args.n, args.concurrency)]
    failures = sum(r["failures"] for r in results)
    for r in results:
        print(f"{r['path']}: {r['requests']} req · failures={r['failures']} · "
              f"p50={r['p50_ms']}ms · max={r['max_ms']}ms")
    print("LOAD PROBE OK" if failures == 0 else "LOAD PROBE FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
