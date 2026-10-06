"""READ-ONLY production deployment preflight.

Runs on the production host before any GrowthOS deploy. It never modifies
anything; it only inspects. A single FAIL aborts deployment (exit code 1).
Checks not applicable to the current OS/environment report SKIPPED.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

from pv_growth.core.config import get_settings

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != FAIL


def _run(cmd: list[str]) -> str | None:
    exe = shutil.which(cmd[0])
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=20)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def is_linux() -> bool:
    return platform.system() == "Linux"


def parse_listening_ports(ss_output: str) -> set[int]:
    """Extract listening TCP/UDP ports from `ss -lntup` output."""
    ports: set[int] = set()
    for line in ss_output.splitlines()[1:]:
        for token in line.split():
            if token.startswith("["):
                token = token[1:]  # "[::]:80" -> "::]:80"
            if ":" in token:
                maybe_port = token.rsplit(":", 1)[-1]
                if maybe_port.isdigit():
                    ports.add(int(maybe_port))
                    break
    return ports


def subnet_overlaps(subnet: str, networks: list[str]) -> str | None:
    """Return the first conflicting network, or None if free."""
    try:
        candidate = ipaddress.ip_network(subnet)
    except ValueError:
        return subnet  # invalid subnet string itself is a conflict
    for net in networks:
        net = net.strip()
        if not net:
            continue
        try:
            other = ipaddress.ip_network(net, strict=False)
        except ValueError:
            continue
        if candidate.version == other.version and candidate.overlaps(other):
            return str(other)
    return None


def collect_route_networks(ip_route_output: str) -> list[str]:
    nets: list[str] = []
    for line in ip_route_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "default":
            nets.append(parts[1] if parts[0] == "dev" or len(parts) > 1 else parts[0])
            nets.append(parts[0])
    return [n for n in nets if "/" in n]


def check_protected_paths(paths: list[str]) -> CheckResult:
    missing = [p for p in paths if not os.path.exists(p)]
    if not is_linux():
        return CheckResult("protected_paths", SKIP, "not linux; expected on dev machines")
    if missing:
        return CheckResult("protected_paths", FAIL, f"missing: {missing}")
    return CheckResult("protected_paths", PASS, f"{len(paths)} present")


def check_protected_containers(containers: list[str]) -> CheckResult:
    if not is_linux():
        return CheckResult("protected_containers", SKIP, "not linux")
    out = _run(["docker", "ps", "--format", "{{.Names}}"])
    if out is None:
        return CheckResult("protected_containers", FAIL, "docker CLI unavailable")
    names = {n.strip() for n in out.splitlines() if n.strip()}
    missing = [c for c in containers if c not in names]
    if missing:
        return CheckResult("protected_containers", FAIL, f"not running: {missing}")
    return CheckResult("protected_containers", PASS, f"{len(containers)} running")


def check_port_free(port: int, ss_output: str | None) -> CheckResult:
    if ss_output is None:
        return CheckResult(
            "growth_port_free",
            SKIP if not is_linux() else FAIL,
            "ss unavailable" if is_linux() else "not linux",
        )
    busy = parse_listening_ports(ss_output)
    if port in busy:
        return CheckResult("growth_port_free", FAIL, f"port {port} already listening")
    return CheckResult("growth_port_free", PASS, f"127.0.0.1:{port} free")


def check_subnet_free(subnet: str, docker_networks: list[str], route_networks: list[str]) -> CheckResult:
    conflict = subnet_overlaps(subnet, docker_networks + route_networks)
    if conflict:
        return CheckResult("growth_subnet_free", FAIL, f"overlaps {conflict}")
    return CheckResult("growth_subnet_free", PASS, f"{subnet} unused")


def check_docker_networks(protected: list[str]) -> tuple[CheckResult, list[str]]:
    if not is_linux():
        return CheckResult("docker_networks", SKIP, "not linux"), []
    out = _run(["docker", "network", "ls", "--format", "{{.Name}}"])
    if out is None:
        return CheckResult("docker_networks", FAIL, "docker CLI unavailable"), []
    names = [n.strip() for n in out.splitlines() if n.strip()]
    missing = [p for p in protected if p not in names]
    if missing:
        return CheckResult("docker_networks", FAIL, f"missing networks: {missing}"), names
    return CheckResult("docker_networks", PASS, f"{len(names)} networks, protected present"), names


def check_duplicate_instance(container_name: str, port: int, ss_output: str | None) -> CheckResult:
    if not is_linux():
        return CheckResult("no_duplicate_instance", SKIP, "not linux")
    out = _run(["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{container_name}$"])
    existing = out is not None and out.strip() == container_name
    port_busy = ss_output is not None and port in parse_listening_ports(ss_output)
    if existing or port_busy:
        return CheckResult(
            "no_duplicate_instance", FAIL, f"container exists={existing}, port busy={port_busy}"
        )
    return CheckResult("no_duplicate_instance", PASS, "none found")


def check_disk(min_mb: int) -> CheckResult:
    if not is_linux():
        return CheckResult("disk_space", SKIP, "not linux")
    usage = shutil.disk_usage("/opt") if os.path.isdir("/opt") else shutil.disk_usage("/")
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_mb:
        return CheckResult("disk_space", FAIL, f"{free_mb}MB free < {min_mb}MB")
    return CheckResult("disk_space", PASS, f"{free_mb}MB free")


def check_memory(min_mb: int) -> CheckResult:
    if not is_linux():
        return CheckResult("memory", SKIP, "not linux")
    try:
        info = dict(
            line.split(":", 1)
            for line in open("/proc/meminfo", encoding="ascii").read().splitlines()
            if ":" in line
        )
        available_kb = int(info["MemAvailable"].strip().split()[0])
    except (OSError, KeyError, ValueError):
        return CheckResult("memory", FAIL, "cannot read /proc/meminfo")
    available_mb = available_kb // 1024
    if available_mb < min_mb:
        return CheckResult("memory", FAIL, f"{available_mb}MB available < {min_mb}MB")
    return CheckResult("memory", PASS, f"{available_mb}MB available")


def check_postgresql_reachable() -> CheckResult:
    try:
        from sqlalchemy import text

        from pv_growth.database.base import get_engine

        engine = get_engine(get_settings())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return CheckResult("postgresql_reachable", PASS, "select 1 ok")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("postgresql_reachable", FAIL, f"{type(exc).__name__}: {exc}"[:200])


def run(args: list[str] | None = None) -> int:
    strict = "--strict" in (args or [])
    container_mode = "--container" in (args or [])
    settings = get_settings()
    if container_mode:
        results = [
            CheckResult("host_checks", SKIP, "container mode; run host gate first"),
            check_disk(settings.min_free_disk_mb),
            check_memory(settings.min_free_mem_mb),
            check_postgresql_reachable(),
        ]
    else:
        ss_out = _run(["ss", "-lntup"]) if is_linux() else None
        net_result, network_names = check_docker_networks(["bridge", "pv_reseller_net", "akhbot_internal"])

        docker_subnets: list[str] = []
        if network_names and "pv_growth_net" in network_names:
            inspect = _run(
                [
                    "docker",
                    "network",
                    "inspect",
                    "pv_growth_net",
                    "--format",
                    "{{(index .IPAM.Config 0).Subnet}}",
                ]
            )
            if inspect:
                docker_subnets.append(inspect.strip())
        routes_out = _run(["ip", "route"]) or ""
        route_nets = collect_route_networks(routes_out) if is_linux() else []

        results = [
            check_protected_paths([p.strip() for p in settings.protected_paths.split(",") if p.strip()]),
            check_protected_containers(
                [c.strip() for c in settings.protected_containers.split(",") if c.strip()]
            ),
            net_result,
            check_port_free(settings.growth_port, ss_out),
            check_subnet_free(settings.growth_subnet, docker_subnets, route_nets),
            check_duplicate_instance(settings.growth_container_name, settings.growth_port, ss_out),
            check_disk(settings.min_free_disk_mb),
            check_memory(settings.min_free_mem_mb),
            check_postgresql_reachable(),
        ]

    print("== PV GrowthOS production preflight (read-only) ==")
    failed = skipped = 0
    for r in results:
        print(f"[{r.status}] {r.name}: {r.detail}")
        failed += r.status == FAIL
        skipped += r.status == SKIP

    if failed or (strict and skipped):
        print(f"PREFLIGHT FAILED ({failed} failed, {skipped} skipped)")
        return 1
    print(f"PREFLIGHT OK ({len(results) - failed - skipped} passed, {skipped} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
