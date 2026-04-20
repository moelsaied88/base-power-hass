"""Sensor platform for Base Power."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import BasePowerCoordinator


@dataclass(frozen=True, kw_only=True)
class BasePowerSensorDescription(SensorEntityDescription):
    """Describes a Base Power sensor and how to pull its value from data."""

    value_fn: Callable[[dict[str, Any]], float | int | None]


SENSORS: tuple[BasePowerSensorDescription, ...] = (
    # stateOfEnergy is an integer Base exposes in their status RPC. It is NOT
    # the public battery state-of-charge - the mobile/web app never displays a
    # battery % - and its exact semantics (grid-services reserve index?) are
    # undocumented. Surface it as-is but label it accordingly so users can
    # build their own heuristics on top.
    BasePowerSensorDescription(
        key="battery_state_of_energy",
        name="Battery State of Energy",
        icon="mdi:battery",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["status"].get("state_of_energy"),
    ),
    BasePowerSensorDescription(
        key="grid_voltage",
        name="Grid Voltage",
        icon="mdi:sine-wave",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["status"].get("grid_voltage") or None,
    ),
    BasePowerSensorDescription(
        key="syn_voltage",
        name="Inverter Synthetic Voltage",
        icon="mdi:sine-wave",
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["status"].get("syn_voltage") or None,
    ),
    BasePowerSensorDescription(
        key="home_power",
        name="Home Power",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (data.get("usage") or {}).get("latest_power_w"),
    ),
    BasePowerSensorDescription(
        key="energy_grid_to_home",
        name="Energy From Grid (recent)",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: (
            (data.get("usage") or {}).get("energy_source_kwh", {}) or {}
        ).get("grid_to_home"),
    ),
    BasePowerSensorDescription(
        key="energy_solar_to_home",
        name="Energy From Solar (recent)",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: (
            (data.get("usage") or {}).get("energy_source_kwh", {}) or {}
        ).get("solar_to_home"),
    ),
    BasePowerSensorDescription(
        key="energy_storage_to_home",
        name="Energy From Battery (recent)",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: (
            (data.get("usage") or {}).get("energy_source_kwh", {}) or {}
        ).get("storage_to_home"),
    ),
    BasePowerSensorDescription(
        key="backup_runtime",
        name="Backup Runtime (at current usage)",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (data.get("usage") or {}).get(
            "latest_duration_hours"
        ),
    ),
    BasePowerSensorDescription(
        key="backup_runtime_at_750w",
        name="Backup Runtime (at 750W low usage)",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (data.get("usage") or {}).get(
            "latest_duration_at_750w_hours"
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BasePowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BasePowerSensor(coordinator, description) for description in SENSORS
    )


class BasePowerSensor(CoordinatorEntity[BasePowerCoordinator], SensorEntity):
    """Generic Base Power sensor driven by a description."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BasePowerCoordinator,
        description: BasePowerSensorDescription,
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
        return self.native_value is not None

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        if not data:
            return None
        try:
            return self.entity_description.value_fn(data)
        except (KeyError, AttributeError, TypeError):
            return None
