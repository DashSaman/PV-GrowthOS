"""Unit tests for preflight logic: port parsing, subnet overlap, route parsing."""

import pv_growth.preflight as preflight
from pv_growth.preflight import (
    PASS,
    CheckResult,
    collect_route_networks,
    parse_listening_ports,
    subnet_overlaps,
)

SS_SAMPLE = """
Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
udp   UNCONN 0      0      0.0.0.0:22295        0.0.0.0:*
tcp   LISTEN 0      511    127.0.0.1:8307       0.0.0.0:*
tcp   LISTEN 0      4096   0.0.0.0:5432         0.0.0.0:*
tcp   LISTEN 0      4096   [::]:80               [::]:*
"""


def test_parse_listening_ports():
    ports = parse_listening_ports(SS_SAMPLE)
    assert {22295, 8307, 5432, 80} <= ports
    assert 22 not in ports


def test_subnet_overlap_detection():
    assert subnet_overlaps("172.23.77.0/24", ["172.17.0.0/16", "172.18.0.0/16", "172.19.0.0/16"]) is None
    assert subnet_overlaps("172.23.0.0/16", ["172.23.77.0/24"]) == "172.23.77.0/24"
    assert subnet_overlaps("172.18.5.0/24", ["172.18.0.0/16"]) == "172.18.0.0/16"
    # invalid candidate subnet counts as a conflict (blocks deploy)
    assert subnet_overlaps("300.1.2.3/24", []) == "300.1.2.3/24"


def test_collect_route_networks():
    routes = collect_route_networks(
        "default via 10.0.0.1 dev eth0\n"
        "172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1\n"
        "10.0.0.0/24 dev eth0 proto kernel scope link src 10.0.0.5\n"
    )
    assert "172.17.0.0/16" in routes
    assert "10.0.0.0/24" in routes
    assert all("default" not in r for r in routes)


def test_container_mode_is_honest_about_host_only_checks(monkeypatch, capsys):
    """An image candidate cannot see host Docker/path state; report that as SKIP."""

    monkeypatch.setattr(preflight, "check_disk", lambda _minimum: CheckResult("disk_space", PASS, "ok"))
    monkeypatch.setattr(preflight, "check_memory", lambda _minimum: CheckResult("memory", PASS, "ok"))
    monkeypatch.setattr(
        preflight,
        "check_postgresql_reachable",
        lambda: CheckResult("postgresql_reachable", PASS, "select 1 ok"),
    )
    monkeypatch.setattr(
        preflight,
        "check_docker_networks",
        lambda _protected: (_ for _ in ()).throw(AssertionError("host Docker check ran in container mode")),
    )

    assert preflight.run(["--container"]) == 0
    output = capsys.readouterr().out
    assert "[SKIP] host_checks: container mode; run host gate first" in output
    assert "[PASS] postgresql_reachable: select 1 ok" in output
