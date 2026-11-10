"""Gateway-offline handling for the ServiceContext payload.

When Base's cloud is reachable but the home gateway is offline, the cloud
still answers MobileGetServiceContext with ``gatewayConnection`` false and
zeros or defaults for everything the gateway would have measured. Those
values are not readings, so they are cleared here and the entities built on
them report ``unavailable``.

This module has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

from typing import Any

# Keys in the normalized ServiceContext dict that come from gateway telemetry.
# ``gateway_connected``, ``has_gateway`` and the wifi fields are not listed:
# they describe the link itself and stay available while the gateway is down.
TELEMETRY_KEYS: tuple[str, ...] = (
    "home_power_w",
    "power_from_grid_w",
    "power_from_storage_w",
    "power_from_solar_w",
    "state_of_energy_pct",
    "state_of_energy_bucket",
    "backup_runtime_hours",
    "backup_runtime_at_750w_hours",
    "grid_voltage",
    "home_has_power",
    "active_outage",
    "active_overcurrent",
    "active_overcurrent_standby",
    "overcurrent_limit",
    "outage_available_backup_hours",
)


def gateway_offline(context: dict[str, Any]) -> bool:
    """Return True only when Base explicitly reports the gateway as offline."""
    return context.get("gateway_connected") is False


def clear_offline_telemetry(context: dict[str, Any]) -> dict[str, Any]:
    """Set every gateway telemetry value to None when the gateway is offline.

    The dict is updated in place and returned. An online (or unknown) gateway
    leaves the context untouched.
    """
    if gateway_offline(context):
        for key in TELEMETRY_KEYS:
            context[key] = None
    return context
