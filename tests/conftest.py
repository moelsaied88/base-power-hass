"""Fixtures: a real Home Assistant core with a fake Base Power client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.base_power import coordinator as coordinator_module
from custom_components.base_power.api import empty_service_context
from custom_components.base_power.api_v2 import (
    empty_billing_cycle,
    empty_overview,
    empty_snapshot,
)
from custom_components.base_power.const import DOMAIN

SERVICE_LOCATION_ID = 49652


# Every entity key that shipped in v0.7.4, with the object id it has on a real
# install. Energy helpers, the Energy Dashboard, and automations depend on them.
V074_SENSORS = {
    "battery_state_of_energy": "base_power_battery_state_of_energy",
    "grid_voltage": "base_power_grid_voltage",
    "home_power": "base_power_home_power",
    "power_from_grid": "base_power_power_from_grid",
    "power_from_storage": "base_power_battery_net_power",
    "power_to_battery": "base_power_power_to_battery",
    "power_from_battery_discharge": "base_power_power_from_battery",
    "power_from_solar": "base_power_power_from_solar",
    "home_from_grid": "base_power_home_from_grid",
    "home_from_battery": "base_power_home_from_battery",
    "home_from_solar": "base_power_home_from_solar",
    "backup_runtime": "base_power_backup_runtime_estimate",
    "usable_battery_energy": "base_power_usable_battery_energy",
    "battery_capacity": "base_power_battery_capacity_at_100",
}
V074_BINARY_SENSORS = {
    "on_battery": "base_power_on_battery",
    "home_has_power": "base_power_home_has_power",
    "grid_connected": "base_power_grid_connected",
    "gateway_connected": "base_power_gateway_connected",
    "overcurrent_active": "base_power_overcurrent_protection_active",
}


def _preregister(hass, entry) -> None:
    """Register entities as a v0.7.4 install has them, under base_power_* ids."""
    registry = er.async_get(hass)
    for domain, keys in (
        ("sensor", V074_SENSORS),
        ("binary_sensor", V074_BINARY_SENSORS),
    ):
        for key, object_id in keys.items():
            registry.async_get_or_create(
                domain,
                DOMAIN,
                f"base_power_{SERVICE_LOCATION_ID}_{key}",
                suggested_object_id=object_id,
                config_entry=entry,
            )


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield


def service_context(**overrides: Any) -> dict[str, Any]:
    """A healthy on-grid reply from get_service_context."""
    context = empty_service_context()
    context.update(
        available=True,
        source="service_context",
        home_power_w=5000.0,
        power_from_grid_w=5400.0,
        power_from_storage_w=-400.0,
        power_from_solar_w=0.0,
        state_of_energy_pct=99.0,
        state_of_energy_bucket=4,
        backup_runtime_hours=8.9,
        backup_runtime_at_750w_hours=59.3,
        home_has_power=True,
        active_outage=False,
        active_overcurrent=False,
        active_overcurrent_standby=False,
        has_gateway=True,
        gateway_connected=True,
    )
    context.update(overrides)
    return context


def snapshot(state: str = "on_grid", **overrides: Any) -> dict[str, Any]:
    result = empty_snapshot()
    result.update(
        available=True,
        state=state,
        observed_at=1000,
        backup_hours=8.9,
        backup_hours_750w=59.3,
        power_flow={
            "from_grid_kw": 5.4,
            "from_storage_kw": -0.4,
            "from_solar_kw": 0.0,
            "non_solar_to_home_kw": 5.0,
            "to_home_kw": 5.0,
        },
    )
    result.update(overrides)
    return result


class Clock:
    """Stands in for time.monotonic in the coordinator's schedules."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    value = Clock()
    with patch.object(coordinator_module, "_monotonic", value):
        yield value


@pytest.fixture
def client(clock):
    """The fake client the coordinator builds. Defaults to a healthy system."""
    with patch.object(coordinator_module, "BasePowerClient") as factory:
        fake = factory.return_value
        fake.get_service_context = AsyncMock(return_value=service_context())
        fake.get_grid_status = AsyncMock(
            return_value={
                "grid_voltage": 245.3,
                "latest_grid_voltage_ts": None,
                "latest_grid_voltage_epoch": None,
                "voltage_samples": [245.3],
            }
        )
        fake.get_recent_usage = AsyncMock(
            return_value={
                "energy_source_kwh": {
                    "grid_to_home": 1.0,
                    "solar_to_home": 0.0,
                    "storage_to_home": 0.5,
                }
            }
        )
        fake.get_battery_snapshot = AsyncMock(return_value=snapshot())
        fake.get_daily_overview = AsyncMock(return_value=empty_overview())
        fake.get_current_billing_cycle = AsyncMock(return_value=empty_billing_cycle())
        fake.get_recent_grid_voltage = AsyncMock(
            return_value={"grid_voltage": None, "latest_grid_voltage_epoch": None}
        )
        yield fake


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    """A config entry titled with the address, the same as a real install."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="123 EXAMPLE ST, HOUSTON TX 77070",
        unique_id=str(SERVICE_LOCATION_ID),
        data={
            "email": "person@example.com",
            "session_id": "sess_secret",
            "client_id": "client_secret",
            "service_location_id": SERVICE_LOCATION_ID,
            "address_id": "addr_secret",
        },
        options={
            "poll_interval_grid": 30,
            "poll_interval_outage": 5,
            "poll_interval_usage": 300,
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
async def setup_integration(hass, entry, client):
    """Return a callable that sets up the entry and returns its coordinator."""

    async def _setup():
        _preregister(hass, entry)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return hass.data.get(DOMAIN, {}).get(entry.entry_id)

    return _setup


@pytest.fixture
def poll(hass, clock):
    """Return a callable that advances the clock and runs one poll."""

    async def _poll(coordinator, seconds: float = 30):
        clock.advance(seconds)
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    return _poll
