"""Network Fault Locator: tells whether network trouble is in the home network, a local device or the ISP."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import NetworkFaultLocatorConfigEntry, NetworkFaultLocatorCoordinator
from .probe import async_detect_privileged

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_FILE = Path(__file__).parent / "www" / "network-fault-locator-card.js"
CARD_URL = f"/{DOMAIN}/network-fault-locator-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard card and load it in the frontend (no extra install needed)."""
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), True)]
    )
    # The version in the URL makes browsers fetch the new card after an update
    add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    return True


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
