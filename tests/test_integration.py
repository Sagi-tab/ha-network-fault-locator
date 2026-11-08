"""Home Assistant tests: config flow, entities, verdicts, events and notification."""

from __future__ import annotations

from datetime import timedelta
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
    "access_points": [f"Living room={AP1}", AP2],
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
    ):
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

        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "name": "Net",
                "router": ROUTER,
                "access_points": [f"Living room={AP1}", " "],
                "internet_targets": [NET1, NET2],
                "dns_host": "google.com",
            },
        )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["title"] == "Net"
    assert done["options"]["access_points"] == [f"Living room={AP1}"]
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
    assert status.state == "mesh_down"
    assert status.attributes["access_points_down"] == ["Living room"]

    net.received[AP1] = 10
    net.received[ROUTER] = 0
    await _tick(hass, freezer)
    assert hass.states.get("sensor.network_status").state == "home_network"


async def test_mesh_packet_loss(hass: HomeAssistant, net: FakeNetwork, freezer) -> None:
    await _setup(hass)
    net.received[AP2] = 8  # 20% loss on one access point, router clean
    await _tick(hass, freezer)
    status = hass.states.get("sensor.network_status")
    assert status.state == "mesh_degraded"
    assert status.attributes["access_points_losing_packets"] == [AP2]
    assert float(hass.states.get("sensor.network_192_168_1_11_packet_loss").state) == 10.0


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


async def test_options_reload(hass: HomeAssistant, net: FakeNetwork) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**{k: v for k, v in OPTIONS.items() if k != "notify_entity"}, "access_points": [AP2]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "notify_entity" not in entry.options
    assert hass.states.get("binary_sensor.network_192_168_1_11").state == "on"
    assert await hass.config_entries.async_unload(entry.entry_id)
