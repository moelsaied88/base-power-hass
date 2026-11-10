"""Tests for gateway-offline handling.

``telemetry.py`` has no Home Assistant imports, so it is loaded by path and
the tests run with plain pytest (no Home Assistant test harness).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "base_power"
    / "telemetry.py"
)
_spec = importlib.util.spec_from_file_location("base_power_telemetry", _PATH)
telemetry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telemetry)


def _online() -> dict:
    return {
        "home_power_w": 1200.0,
        "power_from_grid_w": 1200.0,
        "power_from_storage_w": -300.0,
        "power_from_solar_w": 0.0,
        "state_of_energy_pct": 99.0,
        "state_of_energy_bucket": 4,
        "backup_runtime_hours": 19.3,
        "backup_runtime_at_750w_hours": 30.0,
        "grid_voltage": 247.0,
        "home_has_power": True,
        "active_outage": False,
        "active_overcurrent": False,
        "active_overcurrent_standby": False,
        "overcurrent_limit": None,
        "outage_available_backup_hours": None,
        "has_gateway": True,
        "gateway_connected": True,
        "wifi_ssid": "x",
        "wifi_state": "connected",
    }


def _offline_with_zeros() -> dict:
    ctx = _online()
    ctx.update(
        {
            "home_power_w": 0.0,
            "power_from_grid_w": 0.0,
            "power_from_storage_w": 0.0,
            "power_from_solar_w": 0.0,
            "state_of_energy_pct": 0.0,
            "state_of_energy_bucket": 0,
            "backup_runtime_hours": 0.0,
            "backup_runtime_at_750w_hours": 0.0,
            "grid_voltage": 0.0,
            "home_has_power": False,
            "active_outage": False,
            "gateway_connected": False,
        }
    )
    return ctx


def _offline_with_missing_fields() -> dict:
    ctx = _offline_with_zeros()
    for key in ("state_of_energy_pct", "backup_runtime_hours", "grid_voltage"):
        ctx[key] = None
    ctx["home_has_power"] = None
    return ctx


def _assert_all_cleared(ctx: dict) -> None:
    for key in telemetry.TELEMETRY_KEYS:
        assert ctx[key] is None, key


def test_offline_with_zeros_is_cleared() -> None:
    ctx = telemetry.clear_offline_telemetry(_offline_with_zeros())
    _assert_all_cleared(ctx)


def test_offline_with_missing_fields_is_cleared() -> None:
    ctx = telemetry.clear_offline_telemetry(_offline_with_missing_fields())
    _assert_all_cleared(ctx)


def test_offline_keeps_link_fields() -> None:
    ctx = telemetry.clear_offline_telemetry(_offline_with_zeros())
    assert ctx["gateway_connected"] is False
    assert ctx["has_gateway"] is True
    assert ctx["wifi_state"] == "connected"


def test_online_is_unchanged() -> None:
    ctx = _online()
    assert telemetry.clear_offline_telemetry(ctx) == _online()
    assert not telemetry.gateway_offline(ctx)


def test_online_with_real_zero_soe_is_unchanged() -> None:
    ctx = _online()
    ctx["state_of_energy_pct"] = 0.0
    assert telemetry.clear_offline_telemetry(ctx)["state_of_energy_pct"] == 0.0


def test_unknown_gateway_state_is_unchanged() -> None:
    ctx = _online()
    ctx["gateway_connected"] = None
    assert not telemetry.gateway_offline(ctx)
    assert telemetry.clear_offline_telemetry(ctx)["state_of_energy_pct"] == 99.0


def test_recovery() -> None:
    ctx = telemetry.clear_offline_telemetry(_offline_with_zeros())
    _assert_all_cleared(ctx)
    # The next poll is a fresh dict, so values come back with the gateway.
    fresh = telemetry.clear_offline_telemetry(_online())
    assert fresh["state_of_energy_pct"] == 99.0
    assert fresh["home_has_power"] is True
    assert fresh["active_outage"] is False
