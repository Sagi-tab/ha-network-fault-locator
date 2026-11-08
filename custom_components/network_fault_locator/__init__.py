"""Network Fault Locator: tells whether network trouble is in the home network, the mesh or the ISP."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .coordinator import NetworkFaultLocatorConfigEntry, NetworkFaultLocatorCoordinator
from .probe import async_detect_privileged

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: NetworkFaultLocatorConfigEntry
) -> bool:
    """Set up Network Fault Locator from a config entry."""
    privileged = await async_detect_privileged()
    if privileged is None:
        raise ConfigEntryNotReady("Home Assistant is not allowed to send ICMP pings")

    coordinator = NetworkFaultLocatorCoordinator(hass, entry, privileged)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_reload_on_update(
    hass: HomeAssistant, entry: NetworkFaultLocatorConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: NetworkFaultLocatorConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
