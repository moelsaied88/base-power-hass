"""Sensor platform for Base Power."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
    last_reset_fn: Callable[[dict[str, Any]], datetime | None] | None = None


def _ctx(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("context") or {}


def _cycle(data: dict[str, Any]) -> dict[str, Any]:
    return (data.get("usage") or {}).get("cycle") or {}


def _cents(value: Any) -> float | None:
    return None if value is None else round(int(value) / 100, 2)


def _ts(value: Any) -> datetime | None:
    return None if not value else datetime.fromtimestamp(int(value), tz=UTC)


def _cycle_kwh(data: dict[str, Any]) -> float | None:
    usage = data.get("usage") or {}
    if not usage.get("cycle"):
        return None
    return round(sum(day["home_kwh"] for day in usage.get("days") or []), 3)


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
    # ``suggested_unit_of_measurement=W`` pins the display unit so HA doesn't
    # auto-promote values >= 1000 W to kW. Integration helpers capture the
    # source's displayed unit at creation time, so letting HA flip between W
    # and kW breaks their prefix math (resulting unit becomes ``kkWh``).
    BasePowerSensorDescription(
        key="home_power",
        name="Home Power",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("home_power_w"),
    ),
    BasePowerSensorDescription(
        key="power_from_grid",
        name="Power From Grid",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("power_from_grid_w"),
    ),
    # Signed battery power. Negative = charging (grid/solar pushing INTO the
    # bank), positive = discharging (bank powering the home). Retained for
    # backward compatibility. New installs should prefer the split
    # power_to_battery / power_from_battery_discharge entities below, which are
    # positive-only and feed directly into Energy Dashboard integrators.
    BasePowerSensorDescription(
        key="power_from_storage",
        name="Battery Net Power",
        icon="mdi:battery-charging-outline",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("power_from_storage_w"),
    ),
    BasePowerSensorDescription(
        key="power_to_battery",
        name="Power To Battery",
        icon="mdi:battery-arrow-up",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _charge_power_w(data),
    ),
    BasePowerSensorDescription(
        key="power_from_battery_discharge",
        name="Power From Battery",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _discharge_power_w(data),
    ),
    BasePowerSensorDescription(
        key="power_from_solar",
        name="Power From Solar",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _ctx(data).get("power_from_solar_w"),
    ),
    # Home-allocation sensors. Base's raw ``power_from_grid`` and
    # ``power_from_storage`` are meter-level readings that include arbitrage
    # flows that never touch the home (Base charging the bank from the grid
    # when rates are cheap, or discharging the bank to export during peak).
    # These three derive "what portion of home load each source is actually
    # supplying" using self-consumption priority (solar -> battery -> grid)
    # and are guaranteed always >= 0 and to sum to ``home_power_w``. Feed
    # these into Integration helpers for the Energy Dashboard and arbitrage
    # is excluded by construction.
    BasePowerSensorDescription(
        key="home_from_grid",
        name="Home From Grid",
        icon="mdi:transmission-tower",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _home_alloc(data)["grid"],
    ),
    BasePowerSensorDescription(
        key="home_from_battery",
        name="Home From Battery",
        icon="mdi:home-battery",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _home_alloc(data)["battery"],
    ),
    BasePowerSensorDescription(
        key="home_from_solar",
        name="Home From Solar",
        icon="mdi:solar-power-variant",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _home_alloc(data)["solar"],
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
    # Billing, straight from Base's billing system (GetCurrentBillingCycle)
    # and meter data (GetDailyEnergy). Base's "home kWh" is the amount it
    # bills: the meter reading less its battery adjustment, so these line
    # up with the energy line on the bill. They reset when a new billing cycle
    # starts, so they are TOTAL with last_reset at the cycle start. Use
    # "Bill to Date" as the grid cost entity in the Energy Dashboard so
    # costs there add up to the actual bills.
    BasePowerSensorDescription(
        key="bill_to_date",
        name="Bill to Date",
        icon="mdi:cash",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: _cents(_cycle(data).get("net_accrued_cents")),
        last_reset_fn=lambda data: _ts(_cycle(data).get("service_period_start")),
    ),
    BasePowerSensorDescription(
        key="billed_energy_cycle",
        name="Metered Energy This Cycle",
        icon="mdi:meter-electric",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: _cycle_kwh(data),
        last_reset_fn=lambda data: _ts(_cycle(data).get("service_period_start")),
    ),
)


def _charge_power_w(data: dict[str, Any]) -> float | None:
    """Positive W when the battery bank is charging, else 0.

    Base's ``powerFromStorage`` is signed: negative when grid/solar is pushing
    energy INTO the bank. Clamping ``-signed`` to >= 0 gives a clean charge
    rate that can be fed straight into an ``Integration`` helper for the
    Energy Dashboard's "energy going in to the battery" slot.
    """
    raw = _ctx(data).get("power_from_storage_w")
    if raw is None:
        return None
    try:
        return max(0.0, -float(raw))
    except (TypeError, ValueError):
        return None


def _discharge_power_w(data: dict[str, Any]) -> float | None:
    """Positive W when the battery bank is discharging, else 0."""
    raw = _ctx(data).get("power_from_storage_w")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _home_alloc(data: dict[str, Any]) -> dict[str, float | None]:
    """Allocate live ``home_power_w`` across solar/battery/grid.

    Base's ``powerFlow`` fields are meter-level, so integrating them for the
    Energy Dashboard accidentally counts arbitrage (grid->battery charging or
    battery->grid export). This helper returns the portion of the current
    home load that each source is supplying, using the standard self-
    consumption priority (solar first, then battery discharge, then grid).
    All three values are always non-negative and sum to ``home_power_w``.

    When the battery is charging (``powerFromStorage`` < 0) or the grid is
    exporting (``powerFromGrid`` < 0) that source contributes 0 to the home
    budget - so arbitrage flows don't leak into the dashboard.
    """

    ctx = _ctx(data)
    none_tuple = {"grid": None, "battery": None, "solar": None}

    h_raw = ctx.get("home_power_w")
    if h_raw is None:
        return none_tuple
    try:
        h = max(0.0, float(h_raw))
    except (TypeError, ValueError):
        return none_tuple

    def _pos(v: Any) -> float:
        if v is None:
            return 0.0
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0

    solar_avail = _pos(ctx.get("power_from_solar_w"))
    battery_avail = _pos(ctx.get("power_from_storage_w"))
    grid_avail = _pos(ctx.get("power_from_grid_w"))

    from_solar = min(solar_avail, h)
    remaining = h - from_solar
    from_battery = min(battery_avail, remaining)
    remaining -= from_battery
    from_grid = min(grid_avail, remaining)

    return {
        "grid": round(from_grid, 1),
        "battery": round(from_battery, 1),
        "solar": round(from_solar, 1),
    }


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
    def last_reset(self) -> datetime | None:
        fn = self.entity_description.last_reset_fn
        data = self.coordinator.data
        if fn is None or not data:
            return None
        return fn(data)

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
                "to_battery_w": _charge_power_w(data),
                "discharge_w": _discharge_power_w(data),
            }
        if key == "backup_runtime":
            return {
                "source": "BatteryService.GetSnapshot.backup_hours",
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
        if key in ("bill_to_date", "billed_energy_cycle"):
            cycle = _cycle(data)
            if not cycle:
                return None
            start = _ts(cycle.get("service_period_start"))
            through = _ts(cycle.get("data_through"))
            days = (data.get("usage") or {}).get("days") or []
            return {
                "source": "Base billing (GetCurrentBillingCycle, GetDailyEnergy)",
                "service_period_start": start.isoformat() if start else None,
                "data_through": through.isoformat() if through else None,
                "gross_charges": _cents(cycle.get("gross_charges_cents")),
                "solar_credit": _cents(cycle.get("solar_credit_cents")),
                "metered_kwh": _cycle_kwh(data),
                "daily_kwh": {
                    datetime.fromtimestamp(int(day["start"]), tz=UTC)
                    .astimezone()
                    .date()
                    .isoformat(): round(day["home_kwh"], 2)
                    for day in days
                },
            }
        if key == "battery_state_of_energy":
            return {
                "bucket": ctx.get("state_of_energy_bucket"),
                # "snapshot" = exact value from Base (sent only while off
                # grid); "backup_hours_estimate" = derived from the 750 W
                # backup time and the battery's nameplate size.
                "source": ctx.get("state_of_energy_source"),
                "battery_state": ctx.get("battery_state"),
            }
        return None
