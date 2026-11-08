"""Diagnostics for Network Fault Locator (attach to bug reports).

The ISP first hop reveals the user's ISP and rough location, so it is redacted.
Local addresses and the public targets the user chose are kept: they are needed
to understand a report and identify nobody.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import NetworkFaultLocatorConfigEntry

TO_REDACT = {"isp_hop_address", "address", "notify_entity"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NetworkFaultLocatorConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return async_redact_data(
        {
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
        },
        TO_REDACT,
    )
