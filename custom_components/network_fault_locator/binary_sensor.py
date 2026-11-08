"""Reachability binary sensors: router, each local device, internet and DNS."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NetworkFaultLocatorConfigEntry, NetworkState
from .entity import NetworkFaultLocatorEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetworkFaultLocatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities = [
        ReachabilitySensor(
            coordinator, "router", "router", lambda s: s.snapshot.router.alive,
            lambda s: {"host": coordinator.router},
        ),
        ReachabilitySensor(
            coordinator, "internet", "internet", lambda s: s.snapshot.internet_alive,
            lambda s: {
                "targets": {r.host: r.alive for r in s.snapshot.internet},
            },
        ),
        ReachabilitySensor(
            coordinator, "dns", "dns", lambda s: s.snapshot.dns_ok,
            lambda s: {"host": coordinator.dns_host},
        ),
    ]
    for label, host in coordinator.devices.items():
        entities.append(
            ReachabilitySensor(
                coordinator,
                f"device_{host}",
                "device",
                lambda s, label=label: s.snapshot.devices[label].alive,
                lambda s, host=host: {"host": host},
                {"name": label},
            )
        )
    async_add_entities(entities)


class ReachabilitySensor(NetworkFaultLocatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator,
        key: str,
        translation_key: str,
        value_fn: Callable[[NetworkState], bool],
        attrs_fn: Callable[[NetworkState], dict[str, Any]],
        placeholders: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator, key, translation_key, placeholders)
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn

    @property
    def is_on(self) -> bool:
        return self._value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs_fn(self.coordinator.data)
