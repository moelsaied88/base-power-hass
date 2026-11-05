"""End-to-end behavior through a real Home Assistant core and entity registry."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.base_power.api import (
    BasePowerAuthError,
    BasePowerConnectionError,
    BasePowerProtocolError,
    empty_service_context,
)
from custom_components.base_power.api_v2 import empty_overview
from custom_components.base_power.const import (
    DOMAIN,
    EVENT_OUTAGE_ENDED,
    EVENT_OUTAGE_STARTED,
)
from custom_components.base_power.diagnostics import (
    async_get_config_entry_diagnostics,
)

from pytest_homeassistant_custom_component.common import async_capture_events

from .conftest import V074_BINARY_SENSORS, V074_SENSORS, service_context, snapshot

ON_BATTERY = "binary_sensor.base_power_on_battery"
GRID_CONNECTED = "binary_sensor.base_power_grid_connected"

async def test_upgrade_keeps_every_existing_entity_id(hass, setup_integration):
    await setup_integration()
    for domain, keys in (
        ("sensor", V074_SENSORS),
        ("binary_sensor", V074_BINARY_SENSORS),
    ):
        for object_id in keys.values():
            state = hass.states.get(f"{domain}.{object_id}")
            assert state is not None, object_id
            assert state.state != "unavailable", object_id


async def test_new_entities_use_the_base_power_pattern(
    hass, setup_integration, client
):
    client.get_daily_overview.return_value = {
        **empty_overview(),
        "available": True,
        "cost_today_cents": 1234.0,
    }
    await setup_integration()
    assert hass.states.get("sensor.base_power_battery_state").state == "on_grid"
    assert hass.states.get("binary_sensor.base_power_grid_support").state == "off"
    assert hass.states.get("sensor.base_power_grid_support_events_24h").state == "0"
    assert hass.states.get("sensor.base_power_energy_cost_today").state == "12.34"
    assert hass.states.get("sensor.base_power_newer_api_status").state == "ok"
    assert not [
        state.entity_id
        for state in hass.states.async_all()
        if "example_st" in state.entity_id
        and state.entity_id.split(".")[0] in ("sensor", "binary_sensor")
        and any(
            name in state.entity_id
            for name in ("battery_state", "grid_support", "cost", "bill", "newer_api")
        )
    ]


async def test_healthy_grid_reports_no_outage(hass, setup_integration):
    await setup_integration()
    state = hass.states.get(ON_BATTERY)
    assert state.state == "off"
    assert state.attributes["decided_by"] == "outage_flag"
    assert state.attributes["active_outage_flag"] is False
    assert state.attributes["battery_state"] == "on_grid"
    assert hass.states.get(GRID_CONNECTED).state == "on"


async def test_empty_response_is_unavailable_not_grid_up(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    started = async_capture_events(hass, EVENT_OUTAGE_STARTED)
    ended = async_capture_events(hass, EVENT_OUTAGE_ENDED)

    client.get_service_context.return_value = empty_service_context()
    client.get_grid_status.return_value = {"grid_voltage": None}
    client.get_battery_snapshot.side_effect = BasePowerConnectionError("down")
    await poll(coordinator, 600)

    assert hass.states.get(ON_BATTERY).state == "unavailable"
    assert hass.states.get(GRID_CONNECTED).state == "unavailable"
    assert not started and not ended


async def test_outage_flag_fires_events_and_speeds_up_polling(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    started = async_capture_events(hass, EVENT_OUTAGE_STARTED)
    ended = async_capture_events(hass, EVENT_OUTAGE_ENDED)
    assert coordinator.update_interval == timedelta(seconds=30)

    client.get_service_context.return_value = service_context(active_outage=True)
    await poll(coordinator)
    assert hass.states.get(ON_BATTERY).state == "on"
    assert len(started) == 1
    assert coordinator.update_interval == timedelta(seconds=5)

    client.get_service_context.return_value = service_context()
    client.get_battery_snapshot.return_value = snapshot()
    await poll(coordinator)
    assert hass.states.get(ON_BATTERY).state == "off"
    assert len(ended) == 1
    assert coordinator.update_interval == timedelta(seconds=30)


async def test_unknown_between_outage_readings_keeps_fast_polling_and_no_event(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_service_context.return_value = service_context(active_outage=True)
    await poll(coordinator)
    ended = async_capture_events(hass, EVENT_OUTAGE_ENDED)

    client.get_service_context.return_value = empty_service_context()
    client.get_grid_status.return_value = {"grid_voltage": None}
    client.get_battery_snapshot.side_effect = BasePowerConnectionError("down")
    await poll(coordinator)

    assert hass.states.get(ON_BATTERY).state == "unavailable"
    assert coordinator.update_interval == timedelta(seconds=5)
    assert not ended


async def test_battery_state_detects_an_outage_the_flag_missed(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_battery_snapshot.return_value = snapshot("outage")
    await poll(coordinator, 600)
    state = hass.states.get(ON_BATTERY)
    assert state.state == "on"
    assert state.attributes["decided_by"] == "battery_state"


async def test_failed_snapshot_does_not_hold_an_old_outage(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_battery_snapshot.return_value = snapshot("outage")
    await poll(coordinator, 600)
    assert hass.states.get(ON_BATTERY).state == "on"

    client.get_battery_snapshot.side_effect = BasePowerConnectionError("down")
    await poll(coordinator, 30)
    assert hass.states.get(ON_BATTERY).state == "off"


def _dated_voltage(volts: float, age: float = 0) -> dict:
    return {
        "grid_voltage": volts,
        "latest_grid_voltage_ts": "dated",
        "latest_grid_voltage_epoch": int(time.time() - age),
    }


async def test_low_voltage_needs_two_polls_and_no_fresh_on_grid(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_grid_status.return_value = _dated_voltage(0.0)
    client.get_battery_snapshot.side_effect = BasePowerConnectionError("down")

    await poll(coordinator)
    assert hass.states.get(ON_BATTERY).state == "off"
    await poll(coordinator)
    state = hass.states.get(ON_BATTERY)
    assert state.state == "on"
    assert state.attributes["decided_by"] == "low_voltage"


async def test_fresh_on_grid_snapshot_vetoes_low_voltage(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_grid_status.return_value = _dated_voltage(0.0)
    await poll(coordinator)
    await poll(coordinator)
    await poll(coordinator)
    assert hass.states.get(ON_BATTERY).state == "off"


async def test_old_or_undated_zero_volt_samples_are_not_an_outage(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_battery_snapshot.side_effect = BasePowerConnectionError("down")

    client.get_grid_status.return_value = _dated_voltage(0.0, age=3600)
    await poll(coordinator)
    await poll(coordinator)
    assert hass.states.get(ON_BATTERY).state == "off"

    client.get_grid_status.return_value = {
        "grid_voltage": 0.0,
        "latest_grid_voltage_epoch": None,
    }
    await poll(coordinator)
    await poll(coordinator)
    assert hass.states.get(ON_BATTERY).state == "off"


async def test_snapshot_keeps_entities_alive_when_the_older_api_fails(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_service_context.side_effect = BasePowerProtocolError("404")
    client.get_grid_status.side_effect = BasePowerProtocolError("404")
    client.get_recent_usage.side_effect = BasePowerProtocolError("404")
    await poll(coordinator)

    assert coordinator.last_update_success is True
    assert float(hass.states.get("sensor.base_power_home_power").state) == 5000.0
    assert float(hass.states.get("sensor.base_power_power_to_battery").state) == 400.0
    assert hass.states.get("sensor.base_power_battery_state_of_energy").state == "unavailable"
    state = hass.states.get(ON_BATTERY)
    assert state.state == "off"
    assert state.attributes["decided_by"] == "battery_state"
    status = hass.states.get("sensor.base_power_newer_api_status")
    assert status.attributes["readings_source"] == "v2_snapshot"


async def test_both_apis_failing_marks_the_update_failed(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_service_context.side_effect = BasePowerConnectionError("down")
    client.get_battery_snapshot.side_effect = BasePowerConnectionError("down")
    await poll(coordinator)
    assert coordinator.last_update_success is False
    assert hass.states.get(ON_BATTERY).state == "unavailable"


async def test_older_api_auth_failure_still_opens_reauth(
    hass, entry, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_service_context.side_effect = BasePowerAuthError("expired")
    with patch(
        "custom_components.base_power.config_flow._ClerkAuth.start_sign_in",
        AsyncMock(),
    ):
        await poll(coordinator)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_v2_auth_failure_never_opens_reauth(
    hass, entry, setup_integration, client, poll
):
    client.get_battery_snapshot.side_effect = BasePowerAuthError("rejected")
    client.get_daily_overview.side_effect = BasePowerAuthError("rejected")
    client.get_current_billing_cycle.side_effect = BasePowerAuthError("rejected")
    await setup_integration()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert hass.states.get(ON_BATTERY).state == "off"
    assert hass.states.get("sensor.base_power_newer_api_status").state == "failing"


async def test_v2_parser_surprise_does_not_break_the_poll(
    hass, entry, setup_integration, client
):
    client.get_battery_snapshot.side_effect = KeyError("surprise")
    client.get_daily_overview.side_effect = ValueError("surprise")
    await setup_integration()
    assert entry.state is ConfigEntryState.LOADED
    assert float(hass.states.get("sensor.base_power_home_power").state) == 5000.0
    assert hass.states.get("sensor.base_power_newer_api_status").state == "degraded"


async def test_v2_schedules(hass, setup_integration, client, poll):
    coordinator = await setup_integration()
    assert client.get_battery_snapshot.await_count == 1
    assert client.get_daily_overview.await_count == 1
    assert client.get_current_billing_cycle.await_count == 1

    for _ in range(19):
        await poll(coordinator, 30)
    assert client.get_battery_snapshot.await_count == 1
    await poll(coordinator, 30)
    assert client.get_battery_snapshot.await_count == 2

    assert client.get_daily_overview.await_count == 1
    for _ in range(10):
        await poll(coordinator, 30)
    assert client.get_daily_overview.await_count == 2

    assert client.get_current_billing_cycle.await_count == 1
    await poll(coordinator, 6 * 3600)
    assert client.get_current_billing_cycle.await_count == 2


async def test_suspected_outage_polls_the_snapshot_every_30_seconds(
    hass, setup_integration, client, poll
):
    coordinator = await setup_integration()
    client.get_service_context.return_value = service_context(active_outage=True)
    client.get_battery_snapshot.reset_mock()
    await poll(coordinator, 30)
    assert client.get_battery_snapshot.await_count == 1
    for _ in range(5):
        await poll(coordinator, 5)
    assert client.get_battery_snapshot.await_count == 1
    await poll(coordinator, 5)
    assert client.get_battery_snapshot.await_count == 2


async def test_grid_status_is_skipped_when_context_has_voltage(
    hass, setup_integration, client
):
    client.get_service_context.return_value = service_context(grid_voltage=245.0)
    await setup_integration()
    client.get_grid_status.assert_not_awaited()
    assert hass.states.get("sensor.base_power_grid_voltage").state == "245.0"


async def test_v2_voltage_fills_in_and_is_throttled(
    hass, setup_integration, client, poll
):
    client.get_grid_status.side_effect = BasePowerProtocolError("gone")
    client.get_recent_grid_voltage.return_value = {
        "grid_voltage": 241.0,
        "latest_grid_voltage_epoch": int(time.time()),
    }
    coordinator = await setup_integration()
    assert hass.states.get("sensor.base_power_grid_voltage").state == "241.0"
    state = hass.states.get(ON_BATTERY)
    assert state.attributes["grid_voltage_source"] == "v2"

    await poll(coordinator, 5)
    assert client.get_recent_grid_voltage.await_count == 1
    await poll(coordinator, 30)
    assert client.get_recent_grid_voltage.await_count == 2


async def test_overview_failure_keeps_the_previous_data(
    hass, setup_integration, client, poll
):
    client.get_daily_overview.return_value = {
        **empty_overview(),
        "available": True,
        "grid_support": [{"start": 10, "end": 20}],
    }
    coordinator = await setup_integration()
    assert hass.states.get("sensor.base_power_grid_support_events_24h").state == "1"

    client.get_daily_overview.side_effect = BasePowerConnectionError("down")
    await poll(coordinator, 900)
    assert hass.states.get("sensor.base_power_grid_support_events_24h").state == "1"


async def test_grid_support_is_on_during_an_event(hass, setup_integration, client):
    client.get_daily_overview.return_value = {
        **empty_overview(),
        "available": True,
        "grid_support": [{"start": int(time.time()) - 60, "end": None}],
    }
    await setup_integration()
    assert hass.states.get("binary_sensor.base_power_grid_support").state == "on"


async def test_billing_sensors(hass, setup_integration, client):
    client.get_current_billing_cycle.return_value = {
        "available": True,
        "currency": "USD",
        "service_period_start": 1000,
        "data_through": 2000,
        "gross_charges_cents": 20000,
        "solar_credit_cents": 0,
        "net_accrued_cents": 18853,
        "projected_charges_cents": 30000,
    }
    await setup_integration()
    assert hass.states.get("sensor.base_power_bill_to_date").state == "188.53"
    assert hass.states.get("sensor.base_power_projected_bill").state == "300.0"
    assert hass.states.get("sensor.base_power_solar_buyback_credit").state == "0.0"
    attributes = hass.states.get("sensor.base_power_bill_to_date").attributes
    assert attributes["gross_charges"] == 200.0


async def test_billing_sensors_are_unavailable_without_base_billing(
    hass, setup_integration
):
    await setup_integration()
    assert hass.states.get("sensor.base_power_bill_to_date").state == "unavailable"
    assert hass.states.get("sensor.base_power_energy_cost_today").state == "unavailable"


async def test_diagnostics_redact_account_details(hass, entry, setup_integration):
    await setup_integration()
    dump = str(await async_get_config_entry_diagnostics(hass, entry))
    for secret in ("sess_secret", "client_secret", "addr_secret", "person@example.com"):
        assert secret not in dump
    assert "on_grid" in dump


async def test_first_refresh_fails_cleanly_when_both_apis_are_down(
    hass, entry, setup_integration, client
):
    client.get_service_context = AsyncMock(
        side_effect=BasePowerConnectionError("down")
    )
    client.get_battery_snapshot.side_effect = BasePowerConnectionError("down")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
