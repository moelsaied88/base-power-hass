"""The outage decision. A wrong answer can turn off real loads."""

from __future__ import annotations

import pytest

from custom_components.base_power.outage import resolve_outage


def _resolve(**overrides):
    args = {
        "context_available": True,
        "active_outage_flag": False,
        "voltage": 245.0,
        "low_voltage_polls": 0,
        "snapshot_state": None,
        "snapshot_is_fresh": False,
    }
    args.update(overrides)
    decision = resolve_outage(**args)
    return decision.outage, decision.source


def test_grid_up():
    assert _resolve() == (False, "outage_flag")


def test_flag_means_outage():
    assert _resolve(active_outage_flag=True) == (True, "outage_flag")


@pytest.mark.parametrize(
    "state", ["outage", "no_home_power", "overcurrent", "overcurrent_standby"]
)
def test_off_grid_battery_state_overrides_a_false_flag(state):
    assert _resolve(snapshot_state=state) == (True, "battery_state")


def test_on_grid_snapshot_never_overrides_the_flag():
    result = _resolve(active_outage_flag=True, snapshot_state="on_grid")
    assert result == (True, "outage_flag")


def test_one_low_voltage_poll_is_not_an_outage():
    assert _resolve(voltage=0.0, low_voltage_polls=1) == (False, "outage_flag")


def test_consecutive_low_voltage_polls_are_an_outage():
    assert _resolve(voltage=0.0, low_voltage_polls=2) == (True, "low_voltage")


def test_fresh_on_grid_snapshot_vetoes_low_voltage():
    result = _resolve(
        voltage=0.0,
        low_voltage_polls=2,
        snapshot_state="on_grid",
        snapshot_is_fresh=True,
    )
    assert result == (False, "outage_flag")


def test_stale_on_grid_snapshot_does_not_veto_low_voltage():
    result = _resolve(voltage=0.0, low_voltage_polls=2, snapshot_state="on_grid")
    assert result == (True, "low_voltage")


def test_no_data_is_unknown_not_grid_up():
    result = _resolve(context_available=False, active_outage_flag=None, voltage=None)
    assert result == (None, None)


def test_without_context_on_grid_snapshot_means_grid_up():
    result = _resolve(
        context_available=False,
        active_outage_flag=None,
        voltage=None,
        snapshot_state="on_grid",
    )
    assert result == (False, "battery_state")


def test_without_context_healthy_voltage_means_grid_up():
    result = _resolve(context_available=False, active_outage_flag=None, voltage=240.0)
    assert result == (False, "voltage")


def test_without_context_ambiguous_voltage_is_unknown():
    result = _resolve(context_available=False, active_outage_flag=None, voltage=75.0)
    assert result == (None, None)


def test_telemetry_unavailable_snapshot_alone_is_unknown():
    result = _resolve(
        context_available=False,
        active_outage_flag=None,
        voltage=None,
        snapshot_state="telemetry_unavailable",
    )
    assert result == (None, None)
