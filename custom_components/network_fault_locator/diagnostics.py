"""Diagnostics for Network Fault Locator (attach to bug reports)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import NetworkFaultLocatorConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NetworkFaultLocatorConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "options": dict(entry.options),
        "privileged_icmp": coordinator.privileged,
        "traceroute_available": coordinator.trace_available,
        "last_check": (
            {
                "snapshot": asdict(data.snapshot),
                "verdict": asdict(data.verdict),
                "isp_hop_address": data.isp_hop_address,
                "incident_since": data.incident_since,
            }
            if data
            else None
        ),
    }
