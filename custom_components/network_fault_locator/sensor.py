"""Sensors: fault status, windowed packet loss, latency and the ISP first hop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .analysis import ISSUES, OK
from .coordinator import NetworkFaultLocatorConfigEntry, NetworkState
from .entity import NetworkFaultLocatorEntity

PARALLEL_UPDATES = 0


def _rtt(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetworkFaultLocatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        StatusSensor(coordinator),
        LossSensor(coordinator, "router_loss", "router_loss", lambda s: s.snapshot.router_loss),
        LatencySensor(
            coordinator, "router_latency", "router_latency",
            lambda s: _rtt(s.snapshot.router.avg_rtt),
        ),
        LossSensor(
            coordinator, "internet_loss", "internet_loss", lambda s: s.snapshot.internet_loss
        ),
        LatencySensor(
            coordinator, "internet_latency", "internet_latency",
            lambda s: _rtt(best.avg_rtt) if (best := s.snapshot.best_internet) else None,
        ),
        IspHopSensor(coordinator),
        LossSensor(
            coordinator, "isp_first_hop_loss", "isp_first_hop_loss",
            lambda s: (
                round(100 * (s.snapshot.hop.sent - s.snapshot.hop.received) / s.snapshot.hop.sent, 1)
                if s.snapshot.hop is not None and s.snapshot.hop.sent
                else None
            ),
            available_fn=lambda: coordinator.trace_available,
        ),
    ]
    for label, host in coordinator.access_points.items():
        entities.append(
            LossSensor(
                coordinator, f"ap_{host}_loss", "access_point_loss",
                lambda s, label=label: s.snapshot.access_point_loss.get(label),
                {"name": label},
            )
        )
        entities.append(
            LatencySensor(
                coordinator, f"ap_{host}_latency", "access_point_latency",
                lambda s, label=label: _rtt(s.snapshot.access_points[label].avg_rtt),
                {"name": label},
            )
        )
    async_add_entities(entities)


class StatusSensor(NetworkFaultLocatorEntity, SensorEntity):
    """Most severe current issue, or ok. All issues are in the attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [OK, *ISSUES]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "status", "status")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.verdict.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "issues": data.verdict.issues,
            "access_points_down": data.verdict.access_points_down,
            "access_points_losing_packets": data.verdict.access_points_degraded,
            "isp_first_hop": data.isp_hop_address,
            "incident_since": data.incident_since,
        }


class _ValueSensor(NetworkFaultLocatorEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator,
        key: str,
        translation_key: str,
        value_fn: Callable[[NetworkState], float | None],
        placeholders: dict[str, str] | None = None,
        available_fn: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(coordinator, key, translation_key, placeholders)
        self._value_fn = value_fn
        self._available_fn = available_fn

    @property
    def available(self) -> bool:
        return super().available and (self._available_fn is None or self._available_fn())

    @property
    def native_value(self) -> float | None:
        return self._value_fn(self.coordinator.data)


class LossSensor(_ValueSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1


class LatencySensor(_ValueSensor):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_suggested_display_precision = 1


class IspHopSensor(NetworkFaultLocatorEntity, SensorEntity):
    """Address of the ISP's first router (traceroute hop 2), last known value."""

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "isp_first_hop", "isp_first_hop")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.trace_available

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.isp_hop_address

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hop = self.coordinator.data.snapshot.hop
        return {"responding": hop.alive if hop is not None else None}
