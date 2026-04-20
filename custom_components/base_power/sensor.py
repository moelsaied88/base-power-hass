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


def _ctx(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("context") or {}


SENSORS: tuple[BasePowerSensorDescription, ...] = (
    # stateOfEnergyRaw is the real 0-100 battery % the Base mobile app
    # displays on its Home Energy view. It updates at the primary poll rate.
    BasePowerSensorDescription(
        key="battery_state_of_energy",
        name="Battery State of Energy",
        icon="mdi:battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("state_of_energy_pct"),
    ),
    BasePowerSensorDescription(
        key="grid_voltage",
        name="Grid Voltage",
        icon="mdi:sine-wave",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("grid_voltage"),
    ),
    BasePowerSensorDescription(
        key="home_power",
        name="Home Power",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("home_power_w"),
    ),
    BasePowerSensorDescription(
        key="power_from_grid",
        name="Power From Grid",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("power_from_grid_w"),
    ),
    BasePowerSensorDescription(
        key="power_from_storage",
        name="Power From Battery",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("power_from_storage_w"),
    ),
    BasePowerSensorDescription(
        key="power_from_solar",
        name="Power From Solar",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("power_from_solar_w"),
    ),
    # Base reports live backup runtime directly (what the app displays as
    # "X hrs at current usage"). No more local derivation.
    BasePowerSensorDescription(
        key="backup_runtime",
        name="Backup Runtime (at current usage)",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("backup_runtime_hours"),
    ),
    BasePowerSensorDescription(
        key="backup_runtime_at_750w",
        name="Backup Runtime at 750 W",
        icon="mdi:timer-outline",
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("backup_runtime_at_750w_hours"),
    ),
    BasePowerSensorDescription(
        key="usable_battery_energy",
        name="Usable Battery Energy (remaining)",
        icon="mdi:battery-charging",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _usable_battery_energy_kwh(data),
    ),
    # Derived total capacity. availableBackupAt750W scales linearly with SoE,
    # so dividing remaining kWh by SoE fraction gives the full-charge number.
    # For a dual-battery install this is ~33 kWh (two ~16-17 kWh units).
    BasePowerSensorDescription(
        key="battery_capacity",
        name="Battery Capacity (at 100%)",
        icon="mdi:battery",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _full_battery_capacity_kwh(data),
    ),
    # Energy totals from MobileGetRecentUsage. These are a *rolling window*
    # the Base app uses for its "recent" view - the value can go DOWN as the
    # window slides, so they are MEASUREMENT (not TOTAL_INCREASING). Do NOT
    # plug them into the Energy Dashboard; for that, integrate the live
    # power_from_* sensors via an Integration helper instead.
    BasePowerSensorDescription(
        key="energy_grid_to_home",
        name="Energy From Grid (window)",
        icon="mdi:transmission-tower-export",
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            (data.get("usage") or {}).get("energy_source_kwh", {}) or {}
        ).get("grid_to_home"),
    ),
    BasePowerSensorDescription(
        key="energy_solar_to_home",
        name="Energy From Solar (window)",
        icon="mdi:solar-power",
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            (data.get("usage") or {}).get("energy_source_kwh", {}) or {}
        ).get("solar_to_home"),
    ),
    BasePowerSensorDescription(
        key="energy_storage_to_home",
        name="Energy From Battery (window)",
        icon="mdi:battery-arrow-down",
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            (data.get("usage") or {}).get("energy_source_kwh", {}) or {}
        ).get("storage_to_home"),
    ),
)


def _usable_battery_energy_kwh(data: dict[str, Any]) -> float | None:
    """Return currently-usable battery energy in kWh.

    Base exposes runtime at a constant 750 W load as
    ``availableBackupAt750W`` (hours). Usable kWh = 0.75 * hours. This value
    comes from ServiceContext and updates live. Note: this is what's left
    at the current state of energy, not the battery's total capacity.
    """
    hours = _ctx(data).get("backup_runtime_at_750w_hours")
    if hours is None:
        return None
    return float(hours) * 0.75


def _full_battery_capacity_kwh(data: dict[str, Any]) -> float | None:
    """Return the full-charge usable capacity in kWh.

    Derived from remaining usable kWh / (state of energy / 100). A small SoE
    floor avoids dividing by near-zero when the bank is nearly empty.
    """
    remaining = _usable_battery_energy_kwh(data)
    if remaining is None:
        return None
    soe = _ctx(data).get("state_of_energy_pct")
    if soe is None:
        return None
    try:
        soe_fraction = float(soe) / 100.0
    except (TypeError, ValueError):
        return None
    if soe_fraction < 0.05:
        return None
    return remaining / soe_fraction


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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose context side-channel data for select sensors."""
        data = self.coordinator.data
        if not data:
            return None
        ctx = data.get("context") or {}
        key = self.entity_description.key
        if key == "home_power":
            return {
                "from_grid_w": ctx.get("power_from_grid_w"),
                "from_storage_w": ctx.get("power_from_storage_w"),
                "from_solar_w": ctx.get("power_from_solar_w"),
            }
        if key == "backup_runtime":
            return {
                "source": "MobileGetServiceContext.availableBackup",
                "backup_runtime_at_750w_hours": ctx.get(
                    "backup_runtime_at_750w_hours"
                ),
                "state_of_energy_pct": ctx.get("state_of_energy_pct"),
            }
        if key == "usable_battery_energy":
            return {
                "source_duration_at_750w_hours": ctx.get(
                    "backup_runtime_at_750w_hours"
                ),
                "state_of_energy_pct": ctx.get("state_of_energy_pct"),
            }
        if key == "battery_capacity":
            return {
                "formula": "remaining_kwh / (state_of_energy_pct / 100)",
                "remaining_kwh": _usable_battery_energy_kwh(data),
                "state_of_energy_pct": ctx.get("state_of_energy_pct"),
            }
        if key == "battery_state_of_energy":
            return {
                "bucket": ctx.get("state_of_energy_bucket"),
            }
        return None
