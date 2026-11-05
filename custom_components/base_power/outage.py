"""Decide whether the home is in an outage.

This drives ``binary_sensor.base_power_on_battery``, which can turn off real
loads through automations. Both mistakes are costly, so the rules are explicit:

* Missing data is unknown. It is never reported as "grid is up."
* Base's own signals, the outage flag and the battery state, decide first.
* Low voltage counts only when it is recent, was seen on consecutive polls, and
  a battery snapshot from the same poll doesn't report ``on_grid``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .api_v2 import OFF_GRID_STATES

# Service voltage is about 240 V. 100 V is still grid. Under 50 V the feed has
# collapsed.
GRID_PRESENT_VOLTS = 100.0
GRID_COLLAPSED_VOLTS = 50.0
LOW_VOLTAGE_POLLS_REQUIRED = 2


@dataclass(frozen=True)
class OutageDecision:
    """The decision and the signal that made it."""

    outage: bool | None
    source: str | None


def resolve_outage(
    *,
    context_available: bool,
    active_outage_flag: bool | None,
    voltage: float | None,
    low_voltage_polls: int,
    snapshot_state: str | None,
    snapshot_is_fresh: bool,
) -> OutageDecision:
    """Combine the signals into True (outage), False (grid up), or None."""
    if context_available and active_outage_flag:
        return OutageDecision(True, "outage_flag")
    if snapshot_state in OFF_GRID_STATES:
        return OutageDecision(True, "battery_state")
    if low_voltage_polls >= LOW_VOLTAGE_POLLS_REQUIRED and not (
        snapshot_is_fresh and snapshot_state == "on_grid"
    ):
        return OutageDecision(True, "low_voltage")
    if context_available:
        return OutageDecision(False, "outage_flag")
    if snapshot_state == "on_grid":
        return OutageDecision(False, "battery_state")
    if voltage is not None and voltage >= GRID_PRESENT_VOLTS:
        return OutageDecision(False, "voltage")
    return OutageDecision(None, None)
