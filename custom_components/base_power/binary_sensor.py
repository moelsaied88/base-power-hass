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
    # Set on entities added after v0.7.4 so their entity_id follows the
    # base_power_* pattern instead of the device name, which is the address.
    object_id: str | None = None


def _ctx(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("context") or {}


BINARY_SENSORS: tuple[BasePowerBinaryDescription, ...] = (
    BasePowerBinaryDescription(
        key="on_battery",
        name="On Battery",
        icon="mdi:battery-alert",
        device_class=BinarySensorDeviceClass.POWER,
        is_on_fn=lambda data: _ctx(data).get("outage"),
    ),
    # Track the home's actual power status as reported by Base's gateway.
    # Flips to False during an outage while the house still runs on battery.
    BasePowerBinaryDescription(
        key="home_has_power",
        name="Home Has Power",
        icon="mdi:home-lightning-bolt-outline",
        device_class=BinarySensorDeviceClass.POWER,
        is_on_fn=lambda data: _ctx(data).get("home_has_power"),
    ),
    BasePowerBinaryDescription(
        key="grid_connected",
        name="Grid Connected",
        icon="mdi:transmission-tower",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda data: (
            None if _ctx(data).get("outage") is None else not _ctx(data).get("outage")
        ),
    ),
    BasePowerBinaryDescription(
        key="gateway_connected",
        name="Gateway Connected",
        icon="mdi:wifi",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda data: _ctx(data).get("gateway_connected"),
    ),
    BasePowerBinaryDescription(
        key="overcurrent_active",
        name="Overcurrent Protection Active",
        icon="mdi:flash-alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: _ctx(data).get("active_overcurrent"),
    ),
    BasePowerBinaryDescription(
        key="overcurrent_standby",
        name="Overcurrent Protection Standby",
        icon="mdi:flash-alert-outline",
        entity_registry_enabled_default=False,
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: _ctx(data).get("active_overcurrent_standby"),
    ),
    # The event list refreshes every 15 minutes, so this can lag by that much.
    BasePowerBinaryDescription(
        key="grid_support",
        name="Grid Support",
        icon="mdi:transmission-tower-import",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_on_fn=lambda data: (data.get("v2") or {}).get("grid_support_active"),
        object_id="base_power_grid_support",
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
        if description.object_id:
            self.entity_id = f"binary_sensor.{description.object_id}"
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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Show what decided an outage, so a wrong state can be diagnosed."""
        if self.entity_description.key not in ("on_battery", "grid_connected"):
            return None
        data = self.coordinator.data
        if not data:
            return None
        ctx = _ctx(data)
        return {
            "decided_by": ctx.get("outage_source"),
            "active_outage_flag": ctx.get("active_outage"),
            "grid_voltage_source": ctx.get("grid_voltage_source"),
            "battery_state": ((data.get("v2") or {}).get("snapshot") or {}).get(
                "state"
            ),
        }
