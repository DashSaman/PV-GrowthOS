"""Unit tests for preflight logic: port parsing, subnet overlap, route parsing."""

from pv_growth.preflight import (
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
