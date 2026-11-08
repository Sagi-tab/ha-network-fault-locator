"""Regression tests for the security review (input validation, discovery parsing)."""

import time

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.network_fault_locator.config_flow import (  # noqa: E402
    _validate_devices,
    _validate_network,
    valid_host,
)
from custom_components.network_fault_locator.discovery import read_arp_table  # noqa: E402


def test_hostname_validation_is_linear_time() -> None:
    """The old single-regex check took 84 s for 28 chars + '!' (ReDoS)."""
    start = time.perf_counter()
    for n in (28, 60, 200, 253, 5000):
        assert not valid_host("a" * n + "!")
    assert time.perf_counter() - start < 0.1


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("192.168.1.1", True),
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("router.lan", True),
        ("google.com.", True),
        ("224.0.0.1", False),  # multicast: other hosts answer
        ("0.0.0.0", False),  # unspecified: the local host answers
        ("255.255.255.255", False),
        ("240.0.0.1", False),  # reserved
        ("1.2.3", False),  # malformed IP, not a hostname
        ("-bad.example", False),
        ("bad..example", False),
        ("a b", False),
        ("x" * 64 + ".com", False),
        ("", False),
        ("1.1.1.1; rm -rf /", False),
    ],
)
def test_valid_host(host: str, ok: bool) -> None:
    assert valid_host(host) is ok


def test_limits() -> None:
    base = {"router": "192.168.1.1", "dns_host": "google.com", "internet_targets": ["1.1.1.1"]}
    assert _validate_network(base) == {}
    six = [f"1.1.1.{i}" for i in range(1, 7)]
    assert _validate_network({**base, "internet_targets": six}) == {"internet_targets": "too_many_targets"}
    assert _validate_network({**base, "ping_count": 50, "scan_interval": 30}) == {"scan_interval": "check_too_long"}
    assert _validate_network({**base, "ping_count": 10, "scan_interval": 15}) == {}
    assert _validate_devices([f"10.0.0.{i}" for i in range(31)]) == {"devices": "too_many_devices"}
    assert _validate_devices(["A=10.0.0.1", "A=10.0.0.2"]) == {"devices": "invalid_device"}


def test_read_arp_table(tmp_path) -> None:
    arp = tmp_path / "arp"
    arp.write_text(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.10    0x1         0x2         02:00:00:00:00:14     *        end0\n"
        "192.168.1.7      0x1         0x0         00:00:00:00:00:00     *        end0\n"
        "172.30.33.0      0x1         0x2         ca:fc:05:aa:8b:2b     *        hassio\n"
        "8.8.8.8          0x1         0x2         11:22:33:44:55:66     *        end0\n"
        "garbage line\n"
        "10.0.0.9         0x1         zz          11:22:33:44:55:66     *        end0\n"
    )
    # Incomplete, public and malformed entries are dropped
    assert read_arp_table(str(arp)) == {
        "192.168.1.10": "02:00:00:00:00:14",
        "172.30.33.0": "ca:fc:05:aa:8b:2b",
    }
    assert read_arp_table(str(tmp_path / "missing")) == {}
