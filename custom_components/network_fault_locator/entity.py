"""Base entity for Network Fault Locator."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NetworkFaultLocatorCoordinator


class NetworkFaultLocatorEntity(CoordinatorEntity[NetworkFaultLocatorCoordinator]):
    """All entities of one config entry share a single service device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NetworkFaultLocatorCoordinator,
        key: str,
        translation_key: str,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = translation_key
        if placeholders:
            self._attr_translation_placeholders = placeholders
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Network Fault Locator",
        )
