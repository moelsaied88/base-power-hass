"""Binary sensor platform for Base Power."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import BasePowerCoordinator


@dataclass(frozen=True, kw_only=True)
class BasePowerBinaryDescription(BinarySensorEntityDescription):
    """Describes a Base Power binary sensor."""

    is_on_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSORS: tuple[BasePowerBinaryDescription, ...] = (
    BasePowerBinaryDescription(
        key="on_battery",
        name="On Battery",
        icon="mdi:battery-alert",
        device_class=BinarySensorDeviceClass.POWER,
        is_on_fn=lambda data: data["status"].get("active_outage"),
    ),
    BasePowerBinaryDescription(
        key="grid_connected",
        name="Grid Connected",
        icon="mdi:transmission-tower",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda data: (
            None
            if data["status"].get("active_outage") is None
            else not data["status"]["active_outage"]
        ),
    ),
    BasePowerBinaryDescription(
        key="gateway_connected",
        name="Gateway Connected",
        icon="mdi:wifi",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda data: data["status"].get("gateway_connected"),
    ),
    BasePowerBinaryDescription(
        key="overcurrent_active",
        name="Overcurrent Protection Active",
        icon="mdi:flash-alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: data["status"].get("active_overcurrent"),
    ),
    BasePowerBinaryDescription(
        key="overcurrent_standby",
        name="Overcurrent Protection Standby",
        icon="mdi:flash-alert-outline",
        entity_registry_enabled_default=False,
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: data["status"].get("active_overcurrent_standby"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BasePowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BasePowerBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class BasePowerBinarySensor(
    CoordinatorEntity[BasePowerCoordinator], BinarySensorEntity
):
    """Generic Base Power binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BasePowerCoordinator,
        description: BasePowerBinaryDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        sl_id = coordinator.entry.data.get("service_location_id", "unknown")
        self._attr_unique_id = f"base_power_{sl_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(sl_id))},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=coordinator.entry.title or "Base Power",
            configuration_url="https://account.basepowercompany.com/",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if not data:
            return None
        try:
            return self.entity_description.is_on_fn(data)
        except (KeyError, AttributeError, TypeError):
            return None
