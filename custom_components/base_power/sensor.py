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
    EntityCategory,
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

from .api_v2 import BATTERY_STATES
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import BasePowerCoordinator


@dataclass(frozen=True, kw_only=True)
class BasePowerSensorDescription(SensorEntityDescription):
    """Describes a Base Power sensor and how to pull its value from data."""

    value_fn: Callable[[dict[str, Any]], float | int | str | None]
    # Set on entities added after v0.7.4 so their entity_id follows the
    # base_power_* pattern instead of the device name, which is the address.
    object_id: str | None = None


def _ctx(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("context") or {}


def _v2(data: dict[str, Any], key: str) -> dict[str, Any]:
    return (data.get("v2") or {}).get(key) or {}


def _dollars(cents: float | int | None) -> float | None:
    return None if cents is None else round(cents / 100, 2)


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
    # dashboard.mobile.v2 sensors. Bill and cost amounts need an account billed
    # by Base, and stay unavailable otherwise.
    BasePowerSensorDescription(
        key="battery_state",
        name="Battery State",
        icon="mdi:home-battery",
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_STATES.values()),
        value_fn=lambda data: _v2(data, "snapshot").get("state"),
        object_id="base_power_battery_state",
    ),
    BasePowerSensorDescription(
        key="grid_support_events_24h",
        name="Grid Support Events (24 h)",
        icon="mdi:transmission-tower-import",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            len(_v2(data, "overview")["grid_support"])
            if _v2(data, "overview").get("available")
            else None
        ),
        object_id="base_power_grid_support_events_24h",
    ),
    BasePowerSensorDescription(
        key="energy_cost_today",
        name="Energy Cost Today",
        icon="mdi:currency-usd",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: _dollars(_v2(data, "overview").get("cost_today_cents")),
        object_id="base_power_energy_cost_today",
    ),
    BasePowerSensorDescription(
        key="bill_to_date",
        name="Bill to Date",
        icon="mdi:receipt-text",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: _dollars(
            _v2(data, "billing_cycle").get("net_accrued_cents")
        ),
        object_id="base_power_bill_to_date",
    ),
    BasePowerSensorDescription(
        key="projected_bill",
        name="Projected Bill",
        icon="mdi:receipt-text-clock",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: _dollars(
            _v2(data, "billing_cycle").get("projected_charges_cents")
        ),
        object_id="base_power_projected_bill",
    ),
    BasePowerSensorDescription(
        key="solar_buyback_credit",
        name="Solar Buyback Credit",
        icon="mdi:solar-power",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: _dollars(
            _v2(data, "billing_cycle").get("solar_credit_cents")
        ),
        object_id="base_power_solar_buyback_credit",
    ),
    # Health of the newer API, so a phase-out of either API is visible early.
    BasePowerSensorDescription(
        key="v2_api_status",
        name="Newer API Status",
        icon="mdi:api",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "degraded", "failing"],
        value_fn=lambda data: _v2_api_status(data),
        object_id="base_power_newer_api_status",
    ),
)


def _v2_api_status(data: dict[str, Any]) -> str | None:
    """Summarize v2 calls: all working, some failing, or all failing."""
    health = _v2(data, "health")
    if not health:
        return None
    failing = sum(1 for call in health.values() if call.get("last_error"))
    if not failing:
        return "ok"
    return "failing" if failing == len(health) else "degraded"


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


def _iso(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat()


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
        if description.object_id:
            self.entity_id = f"sensor.{description.object_id}"
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
                "to_battery_w": _charge_power_w(data),
                "discharge_w": _discharge_power_w(data),
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
        if key == "battery_state":
            snapshot = _v2(data, "snapshot")
            return {
                "observed_at": _iso(snapshot.get("observed_at")),
                "state_of_energy_percent": snapshot.get("state_of_energy_percent"),
                "overcurrent_limit_kw": snapshot.get("overcurrent_limit_kw"),
                "backup_hours": snapshot.get("backup_hours"),
                "backup_hours_750w": snapshot.get("backup_hours_750w"),
                "power_flow": snapshot.get("power_flow"),
            }
        if key == "grid_support_events_24h":
            intervals = _v2(data, "overview").get("grid_support") or []
            if not intervals:
                return None
            latest = max(intervals, key=lambda item: item.get("start") or 0)
            return {
                "last_start": _iso(latest.get("start")),
                "last_end": _iso(latest.get("end")),
            }
        if key == "energy_cost_today":
            return {"energy_mix_24h": _v2(data, "overview").get("energy_mix_24h")}
        if key in ("bill_to_date", "projected_bill", "solar_buyback_credit"):
            cycle = _v2(data, "billing_cycle")
            return {
                "service_period_start": _iso(cycle.get("service_period_start")),
                "data_through": _iso(cycle.get("data_through")),
                "gross_charges": _dollars(cycle.get("gross_charges_cents")),
            }
        if key == "v2_api_status":
            return {
                "calls": _v2(data, "health"),
                "readings_source": ctx.get("source"),
            }
        return None
