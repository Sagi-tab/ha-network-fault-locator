"""Home Assistant tests: config flow, entities, verdicts, events and notification."""

from __future__ import annotations

from datetime import timedelta
import re
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.network_fault_locator.analysis import HopResult, ProbeResult  # noqa: E402
from custom_components.network_fault_locator.const import (  # noqa: E402
    DOMAIN,
    EVENT_INCIDENT_RESOLVED,
    EVENT_INCIDENT_STARTED,
    EVENT_ISP_HOP_CHANGED,
)

PKG = "custom_components.network_fault_locator"
ROUTER, AP1, AP2, NET1, NET2 = "192.168.1.1", "192.168.1.10", "192.168.1.11", "1.1.1.1", "8.8.8.8"

OPTIONS = {
    "router": ROUTER,
    "devices": [f"Living room={AP1}", AP2],
    "internet_targets": [NET1, NET2],
    "dns_host": "google.com",
    "notify_entity": "notify.phone",
    "ping_count": 10,
    "scan_interval": 30,
    "loss_window": 10,
    "loss_threshold": 2.0,
    "min_incident": 60,
}


class FakeNetwork:
    """Mutable network state the patched probes read from."""

    def __init__(self) -> None:
        self.received = {h: 10 for h in (ROUTER, AP1, AP2, NET1, NET2)}
        self.dns = True
        self.hop = HopResult("203.0.113.1", 5, 5)

    async def ping(self, hosts, count, privileged):
        return {
            h: ProbeResult(h, count, self.received.get(h, 0), 1.0 if self.received.get(h) else None)
            for h in hosts
        }

    async def dns_ok(self, host):
        return self.dns

    def trace(self, target):
        return ROUTER, self.hop


@pytest.fixture
def net():
    network = FakeNetwork()
    with (
        patch(f"{PKG}.async_detect_privileged", AsyncMock(return_value=True)),
        patch(f"{PKG}.coordinator.async_ping_hosts", side_effect=network.ping),
        patch(f"{PKG}.coordinator.async_dns_ok", side_effect=network.dns_ok),
        patch(f"{PKG}.coordinator.trace_first_hops", side_effect=network.trace),
    ):
        yield network


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Network", data={"name": "Network"}, options=OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tick(hass: HomeAssistant, freezer) -> None:
    """Advance the clock past one check interval (plus the coordinator's jitter)."""
    freezer.tick(timedelta(seconds=31))
    # The frozen clock doesn't move the event loop's timer, so fire it explicitly
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    # Scheduled coordinator refreshes run as background tasks
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_config_flow(hass: HomeAssistant) -> None:
    with (
        patch(f"{PKG}.config_flow.async_detect_privileged", AsyncMock(return_value=True)),
        patch(f"{PKG}.config_flow.async_detect_router", AsyncMock(return_value=ROUTER)),
        patch(f"{PKG}.async_setup_entry", AsyncMock(return_value=True)),
        patch(
            f"{PKG}.discovery.read_arp_table",
            return_value={AP1: "aa:bb:cc:dd:ee:01", AP2: "aa:bb:cc:dd:ee:02", ROUTER: "aa:bb:cc:dd:ee:00"},
        ),
    ):
        # A device Home Assistant knows by MAC gets its name in the picker
        cfg = MockConfigEntry(domain="test")
        cfg.add_to_hass(hass)
        dr.async_get(hass).async_get_or_create(
            config_entry_id=cfg.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, "AA:BB:CC:DD:EE:01")},
            name="Mesh node living room",
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        # The router field is pre-filled with the detected first hop
        schema = {str(k): k.default() for k in result["data_schema"].schema if hasattr(k, "default") and callable(k.default)}
        assert schema["router"] == ROUTER

        bad = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "Net", "router": "not a host!", "internet_targets": [NET1], "dns_host": "google.com"},
        )
        assert bad["errors"] == {"router": "invalid_host"}

        devices = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "Net", "router": ROUTER, "internet_targets": [NET1, NET2], "dns_host": "google.com"},
        )
        assert devices["step_id"] == "devices"
        offered = {
            o["value"]: o["label"]
            for o in devices["data_schema"].schema["devices"].config["options"]
        }
        # Router excluded; known device named; unknown one shown with its MAC
        assert offered == {
            f"Mesh node living room={AP1}": f"Mesh node living room ({AP1})",
            AP2: f"{AP2} (aa:bb:cc:dd:ee:02)",
        }

        bad = await hass.config_entries.flow.async_configure(
            devices["flow_id"], {"devices": ["Broken=not a host!"]}
        )
        assert bad["errors"] == {"devices": "invalid_device"}

        done = await hass.config_entries.flow.async_configure(
            devices["flow_id"], {"devices": [f"Mesh node living room={AP1}", "Switch=192.168.1.5", " "]}
        )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["title"] == "Net"
    assert done["options"]["devices"] == [f"Mesh node living room={AP1}", "Switch=192.168.1.5"]
    assert done["options"]["internet_targets"] == [NET1, NET2]
    assert done["options"]["ping_count"] == 10


async def test_config_flow_aborts_without_icmp(hass: HomeAssistant) -> None:
    with patch(f"{PKG}.config_flow.async_detect_privileged", AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_icmp"


async def test_entities_and_ok_state(hass: HomeAssistant, net: FakeNetwork) -> None:
    await _setup(hass)
    status = hass.states.get("sensor.network_status")
    assert status.state == "ok"
    assert status.attributes["isp_first_hop"] == "203.0.113.1"
    assert hass.states.get("binary_sensor.network_router").state == "on"
    assert hass.states.get("binary_sensor.network_living_room").state == "on"
    assert hass.states.get("binary_sensor.network_192_168_1_11").state == "on"
    assert hass.states.get("sensor.network_living_room_packet_loss").state == "0.0"
    assert hass.states.get("sensor.network_router_latency").state == "1.0"
    assert hass.states.get("sensor.network_isp_first_hop").state == "203.0.113.1"


async def test_verdicts(hass: HomeAssistant, net: FakeNetwork, freezer) -> None:
    await _setup(hass)

    net.received[NET1] = net.received[NET2] = 0
    net.hop = HopResult(None, 5, 0)
    await _tick(hass, freezer)
    assert hass.states.get("sensor.network_status").state == "isp_line"
    assert hass.states.get("binary_sensor.network_internet").state == "off"
    # Last known first hop is kept while it's silent
    assert hass.states.get("sensor.network_isp_first_hop").state == "203.0.113.1"

    net.hop = HopResult("203.0.113.1", 5, 5)
    await _tick(hass, freezer)
    assert hass.states.get("sensor.network_status").state == "isp_upstream"

    net.received[NET1] = net.received[NET2] = 10
    net.received[AP1] = 0
    await _tick(hass, freezer)
    status = hass.states.get("sensor.network_status")
    assert status.state == "device_down"
    assert status.attributes["devices_down"] == ["Living room"]

    net.received[AP1] = 10
    net.received[ROUTER] = 0
    await _tick(hass, freezer)
    assert hass.states.get("sensor.network_status").state == "home_network"


async def test_mesh_packet_loss(hass: HomeAssistant, net: FakeNetwork, freezer) -> None:
    net.received[AP2] = 8  # 20% loss on one access point, router clean
    await _setup(hass)  # first lossy check
    assert hass.states.get("sensor.network_status").state == "ok"  # one check isn't enough
    await _tick(hass, freezer)  # second lossy check
    status = hass.states.get("sensor.network_status")
    assert status.state == "device_degraded"
    assert status.attributes["devices_losing_packets"] == [AP2]
    assert float(hass.states.get("sensor.network_192_168_1_11_packet_loss").state) == 20.0


async def test_packet_loss_burst_and_report(hass: HomeAssistant, net: FakeNetwork, freezer) -> None:
    """A one-check burst raises nothing; repeated loss reports when it happened."""
    started = async_capture_events(hass, EVENT_INCIDENT_STARTED)
    resolved = async_capture_events(hass, EVENT_INCIDENT_RESOLVED)
    notify_calls = async_mock_service(hass, "notify", "send_message")
    await _setup(hass)

    net.received[NET1] = net.received[NET2] = 3  # one bad check on both targets
    await _tick(hass, freezer)
    net.received[NET1] = net.received[NET2] = 10
    for _ in range(3):
        await _tick(hass, freezer)
    assert hass.states.get("sensor.network_status").state == "ok"
    assert float(hass.states.get("sensor.network_internet_packet_loss").state) > 2
    assert started == []

    net.received[NET1] = net.received[NET2] = 6  # now loss in two more checks
    await _tick(hass, freezer)
    await _tick(hass, freezer)
    net.received[NET1] = net.received[NET2] = 10
    await _tick(hass, freezer)
    assert hass.states.get("sensor.network_status").state == "isp_degraded"
    assert started[0].data["issues"] == ["isp_degraded"]

    for _ in range(25):  # until the lossy checks leave the 10-minute window
        await _tick(hass, freezer)
    assert hass.states.get("sensor.network_status").state == "ok"
    incident = resolved[0].data
    item = incident["issues"][0]
    assert item["issue"] == "isp_degraded" and item["packet_loss"] is True
    # The report covers the lossy checks (the first burst through the last
    # loss, about 2.5 min), not the ~13 minutes the verdict was up
    assert 120 <= incident["duration"] <= 160
    assert incident["active_duration"] > 600
    assert re.search(r"^• ISP packet loss: \d\d:\d\d–\d\d:\d\d$", incident["summary"], re.M)
    assert len(notify_calls) == 1


async def test_incident_events_and_notify(hass: HomeAssistant, net: FakeNetwork, freezer) -> None:
    started = async_capture_events(hass, EVENT_INCIDENT_STARTED)
    resolved = async_capture_events(hass, EVENT_INCIDENT_RESOLVED)
    hop_changed = async_capture_events(hass, EVENT_ISP_HOP_CHANGED)
    notify_calls = async_mock_service(hass, "notify", "send_message")
    await _setup(hass)

    net.received[NET1] = net.received[NET2] = 0
    net.hop = HopResult(None, 5, 0)
    await _tick(hass, freezer)
    assert len(started) == 1 and started[0].data["issues"] == ["isp_line"]

    for _ in range(3):  # still down
        await _tick(hass, freezer)
    net.received[NET1] = net.received[NET2] = 10
    net.hop = HopResult("203.0.113.2", 5, 5)  # came back through a new gateway
    await _tick(hass, freezer)

    assert len(resolved) == 1
    incident = resolved[0].data
    assert incident["issues"][0]["issue"] == "isp_line"
    assert incident["duration"] >= 60
    assert "ISP line / access" in incident["summary"]
    assert len(notify_calls) == 1
    assert notify_calls[0].data["entity_id"] == "notify.phone"
    assert hop_changed[0].data["old"] == "203.0.113.1" and hop_changed[0].data["new"] == "203.0.113.2"


async def test_short_incident_not_reported(hass: HomeAssistant, net: FakeNetwork, freezer) -> None:
    resolved = async_capture_events(hass, EVENT_INCIDENT_RESOLVED)
    await _setup(hass)
    net.dns = False
    await _tick(hass, freezer)
    net.dns = True
    await _tick(hass, freezer)  # ~31 s incident < 60 s minimum
    assert resolved == []


async def test_options_flow(hass: HomeAssistant, net: FakeNetwork) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    network = {k: v for k, v in OPTIONS.items() if k not in ("devices", "notify_entity")}

    too_long = await hass.config_entries.options.async_configure(
        result["flow_id"], {**network, "ping_count": 30, "scan_interval": 20}
    )
    assert too_long["errors"] == {"scan_interval": "check_too_long"}

    with patch(f"{PKG}.discovery.read_arp_table", return_value={}):
        devices = await hass.config_entries.options.async_configure(result["flow_id"], network)
        assert devices["step_id"] == "devices"
        # Currently watched devices stay selectable even when not discovered
        assert devices["data_schema"].schema["devices"].config["options"][0] == {
            "value": f"Living room={AP1}", "label": f"Living room ({AP1})"
        }
        done = await hass.config_entries.options.async_configure(devices["flow_id"], {"devices": [AP2]})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "notify_entity" not in entry.options  # clearing the notify field sticks
    assert entry.options["devices"] == [AP2]
    assert hass.states.get("binary_sensor.network_192_168_1_11").state == "on"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_one_bad_target_does_not_break_checks(hass: HomeAssistant) -> None:
    """A target icmplib refuses only marks itself down (real probe code, mocked icmplib)."""
    from icmplib import ICMPLibError

    from custom_components.network_fault_locator import probe

    class Reply:
        def __init__(self, address):
            self.address, self.packets_sent, self.packets_received = address, 3, 3
            self.avg_rtt, self.is_alive = 1.0, True

    async def single(host, **kwargs):
        if host == "bad":
            raise ICMPLibError("refused")
        return Reply(host)

    with (
        patch.object(probe, "async_multiping", AsyncMock(side_effect=ICMPLibError("refused"))),
        patch.object(probe, "async_ping", side_effect=single),
    ):
        results = await probe.async_ping_hosts([ROUTER, "bad", NET1], 3, True)
    assert results[ROUTER].alive and results[NET1].alive
    assert not results["bad"].alive and results["bad"].sent == 3


async def test_corrupt_storage_is_ignored(hass: HomeAssistant, net: FakeNetwork, hass_storage) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Network", data={"name": "Network"}, options=OPTIONS)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"incident": {"since": "garbage", "issues": {"isp_line": 5}}, "isp_hop_address": 42},
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.network_status").state == "ok"


async def test_diagnostics_redact_isp_hop(hass: HomeAssistant, net: FakeNetwork) -> None:
    from custom_components.network_fault_locator.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "203.0.113.1" not in text and "notify.phone" not in text
    assert diag["last_check"]["isp_hop_address"] == "**REDACTED**"


async def test_card_is_served_and_registered(hass: HomeAssistant, net: FakeNetwork, frontend) -> None:
    """The dashboard card ships with the integration: no separate frontend install."""
    from pathlib import Path

    await _setup(hass)
    (paths,), _ = frontend["http"].async_register_static_paths.call_args
    assert paths[0].url_path == "/network_fault_locator/network-fault-locator-card.js"
    assert Path(paths[0].path).is_file()
    (_, url), _ = frontend["extra_js"].call_args
    assert url.startswith("/network_fault_locator/network-fault-locator-card.js?v=")
