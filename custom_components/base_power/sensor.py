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
    # Live-computed to match the Base app's "X hrs at current usage" number.
    # We derive usable battery energy from Base's constant
    # duration_at_750w_hours (kWh = 0.75 * hours) and divide by our actual
    # Home Power reading. Base's mobile app uses the exact same formula:
    #   - 10 kWh usable / 6.5 kW live = 1.54 h  ("~1.5 hrs at high usage")
    #   - 10 kWh usable / 0.75 kW     = 13.3 h  ("~13.3 hrs with low usage")
    BasePowerSensorDescription(
        key="backup_runtime",
        name="Backup Runtime (at current usage)",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _derive_backup_runtime_hours(data),
    ),
    BasePowerSensorDescription(
        key="usable_battery_energy",
        name="Usable Battery Energy",
        icon="mdi:battery-charging",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _usable_battery_energy_kwh(data),
    ),
)


def _usable_battery_energy_kwh(data: dict[str, Any]) -> float | None:
    """Return the currently-usable battery energy in kWh.

    Derived from Base's ``duration_at_750w`` field, which represents hours
    of backup at a constant 750W load - i.e. usable_energy_kwh = hours * 0.75.
    """
    usage = data.get("usage") or {}
    hours = usage.get("latest_duration_at_750w_hours")
    if hours is None:
        return None
    return float(hours) * 0.75


def _derive_backup_runtime_hours(data: dict[str, Any]) -> float | None:
    """Compute remaining backup runtime at current Home Power load.

    Uses the same formula as the Base mobile app:
        runtime_hours = usable_battery_energy_kwh / current_home_power_kw

    Returns None when we have no usable energy or no recent power sample.
    Clamps home power to at least 100 W so that ultra-low draws don't blow
    up to absurd runtime estimates (the app does the same kind of guard).
    """
    usable = _usable_battery_energy_kwh(data)
    if usable is None or usable <= 0:
        return None
    usage = data.get("usage") or {}
    power_w = usage.get("latest_power_w")
    if power_w is None:
        return None
    power_kw = max(float(power_w), 100.0) / 1000.0
    return usable / power_kw


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
        """Expose timeseries + freshness attributes for select sensors.

        Lets users see Base's raw duration/power points directly in HA dev
        tools so they can verify readings against the Base app.
        """
        data = self.coordinator.data
        if not data:
            return None
        usage = data.get("usage") or {}
        key = self.entity_description.key
        if key == "home_power":
            return {"latest_sample_ts": usage.get("latest_power_ts")}
        if key == "backup_runtime":
            return {
                "formula": "usable_energy_kwh / home_power_kw",
                "usable_battery_energy_kwh": _usable_battery_energy_kwh(data),
                "home_power_w": usage.get("latest_power_w"),
                "base_reported_duration_hours": usage.get(
                    "latest_duration_hours"
                ),
            }
        if key == "usable_battery_energy":
            return {
                "source_duration_at_750w_hours": usage.get(
                    "latest_duration_at_750w_hours"
                ),
            }
        return None
