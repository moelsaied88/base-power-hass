"""DataUpdateCoordinator for Base Power.

Primary poll: ``MobileGetServiceContext`` and ``MobileGetGridStatus`` on the
mobile API host (``dashboard.baseapis.net``). They return powerFlow (kW),
stateOfEnergyRaw (%), availableBackup (hours at current draw), gridVoltage, and
outage flags. They run every ``poll_interval_grid`` seconds, dropping to
``poll_interval_outage`` seconds when the home is on battery.

Secondary poll: ``MobileGetRecentUsage`` runs at ``poll_interval_usage`` for the
rolling energy-by-source sensors.

Newer API: Base's current app uses ``dashboard.mobile.v2``. These calls add data
and act as a fallback, on fixed schedules:

* ``GetSnapshot`` every 10 minutes, every 30 seconds while something points to
  an outage or the older API isn't answering
* ``GetDailyOverview`` every 15 minutes
* ``GetCurrentBillingCycle`` every 6 hours
* ``GetRecentGridVoltage`` only when the older calls have no voltage, at most
  every 30 seconds

A v2 failure is logged at debug and never fails a poll or opens a re-auth
prompt. If the older ``MobileGetServiceContext`` call fails, the snapshot fills
in its readings so the older API can go away without taking the entities with
it.

On outage transitions we fire ``base_power_outage_started`` /
``base_power_outage_ended`` on the HA event bus so automations can react.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import (
    BasePowerAuthError,
    BasePowerClient,
    BasePowerConnectionError,
    BasePowerError,
    BasePowerProtocolError,
    ServiceLocation,
    empty_service_context,
)
from .api_v2 import (
    OFF_GRID_STATES,
    empty_billing_cycle,
    empty_overview,
    empty_snapshot,
    grid_support_active,
)
from .const import (
    CONF_ADDRESS_ID,
    CONF_CLIENT_ID,
    CONF_EMAIL,
    CONF_POLL_INTERVAL_GRID,
    CONF_POLL_INTERVAL_OUTAGE,
    CONF_POLL_INTERVAL_USAGE,
    CONF_SERVICE_LOCATION_ID,
    CONF_SESSION_ID,
    DEFAULT_POLL_INTERVAL_GRID,
    DEFAULT_POLL_INTERVAL_OUTAGE,
    DEFAULT_POLL_INTERVAL_USAGE,
    EVENT_OUTAGE_ENDED,
    EVENT_OUTAGE_STARTED,
    V2_BILLING_INTERVAL,
    V2_OVERVIEW_INTERVAL,
    V2_SNAPSHOT_INTERVAL,
    V2_SNAPSHOT_INTERVAL_SUSPECT,
    V2_VOLTAGE_MIN_INTERVAL,
)
from .outage import GRID_COLLAPSED_VOLTS, resolve_outage

_LOGGER = logging.getLogger(__name__)

# A timestamped voltage sample older than this can't indicate a current outage.
_VOLTAGE_MAX_AGE_SECONDS = 600


def _monotonic() -> float:
    """Clock for call schedules. Tests replace this."""
    return time.monotonic()


class BasePowerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates Base Power polling with adaptive intervals + events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        self.entry = entry
        self._grid_interval = timedelta(
            seconds=entry.options.get(
                CONF_POLL_INTERVAL_GRID,
                DEFAULT_POLL_INTERVAL_GRID.total_seconds(),
            )
        )
        self._outage_interval = timedelta(
            seconds=entry.options.get(
                CONF_POLL_INTERVAL_OUTAGE,
                DEFAULT_POLL_INTERVAL_OUTAGE.total_seconds(),
            )
        )
        # Usage (power/energy) data fetch cadence. Controls how often we call
        # MobileGetRecentUsage.
        self._usage_interval = timedelta(
            seconds=entry.options.get(
                CONF_POLL_INTERVAL_USAGE,
                DEFAULT_POLL_INTERVAL_USAGE.total_seconds(),
            )
        )
        # The HA DataUpdateCoordinator ticks at the smallest of the three
        # intervals so every source can be refreshed at its configured rate.
        initial_interval = min(self._grid_interval, self._usage_interval)

        super().__init__(
            hass,
            _LOGGER,
            name=f"Base Power ({entry.data.get(CONF_EMAIL, 'unknown')})",
            update_interval=initial_interval,
        )

        session = aiohttp_client.async_get_clientsession(hass)
        self.client = BasePowerClient(
            session,
            email=entry.data.get(CONF_EMAIL),
            session_id=entry.data.get(CONF_SESSION_ID),
            client_id=entry.data.get(CONF_CLIENT_ID),
        )
        self._location: ServiceLocation | None = None
        self._last_usage_fetch: float = 0.0
        self._last_outage_state: bool | None = None
        self._last_usage: dict[str, Any] = {}

        self._low_voltage_polls = 0
        self._snapshot: dict[str, Any] = empty_snapshot()
        self._overview: dict[str, Any] = empty_overview()
        self._billing_cycle: dict[str, Any] = empty_billing_cycle()
        # Monotonic time of the last attempt. None means never tried.
        self._last_snapshot_fetch: float | None = None
        self._last_overview_fetch: float | None = None
        self._last_billing_fetch: float | None = None
        self._last_v2_voltage_fetch: float | None = None
        self._v2_health: dict[str, dict[str, Any]] = {}

    async def _ensure_auth_and_location(self) -> ServiceLocation:
        if self._location is None:
            address_id = self.entry.data.get(CONF_ADDRESS_ID)
            stored_sl_id = self.entry.data.get(CONF_SERVICE_LOCATION_ID)
            if address_id and stored_sl_id:
                self._location = ServiceLocation(
                    service_location_id=int(stored_sl_id),
                    address_id=str(address_id),
                    address_display=self.entry.title or "Base Power",
                    has_gateway=True,
                    has_solar=False,
                    timezone="",
                )
            else:
                raise UpdateFailed(
                    "Config entry is missing address_id/service_location_id"
                )
        return self._location

    async def _async_update_data(self) -> dict[str, Any]:
        location = await self._ensure_auth_and_location()

        context = empty_service_context()
        context_error: BasePowerError | None = None
        try:
            context = dict(
                await self.client.get_service_context(location.service_location_id)
            )
        except BasePowerAuthError as exc:
            # Clerk session expired/revoked - trigger reauth flow in the UI.
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except BasePowerError as exc:
            context_error = exc
            _LOGGER.debug("service_context fetch failed: %s", exc)

        await self._async_fill_voltage(context, location)
        self._track_low_voltage(context)
        snapshot_is_fresh = await self._async_refresh_snapshot(context, location)

        if not context["available"] and self._snapshot.get("available"):
            _apply_snapshot_fallback(context, self._snapshot)
        elif context_error is not None:
            # Neither API answered. Report it the way the older API did.
            raise _update_failed(context_error) from context_error

        decision = resolve_outage(
            context_available=context["available"],
            active_outage_flag=context["active_outage"],
            voltage=context["grid_voltage"],
            low_voltage_polls=self._low_voltage_polls,
            snapshot_state=self._snapshot.get("state"),
            snapshot_is_fresh=snapshot_is_fresh,
        )
        context["outage"] = decision.outage
        context["outage_source"] = decision.source

        usage = self._last_usage
        now = _monotonic()
        usage_interval_s = self._usage_interval.total_seconds()
        # ServiceContext doesn't have energy-by-source totals, so we keep a
        # slower secondary poll of MobileGetRecentUsage. Defaults to 5 min.
        if now - self._last_usage_fetch >= usage_interval_s - 0.5:
            try:
                usage = await self.client.get_recent_usage(location.address_id)
                self._last_usage = usage
                self._last_usage_fetch = now
            except BasePowerError as exc:
                _LOGGER.debug("recent_usage fetch failed: %s", exc)

        await self._async_refresh_slow_v2(location)

        self._maybe_fire_outage_event(decision.outage)
        self._apply_adaptive_interval(
            decision.outage
            if decision.outage is not None
            else bool(self._last_outage_state)
        )

        return {
            "context": context,
            # Kept under "status" too for any external automations/templates
            # that were reading from it before. Field names unchanged.
            "status": {
                "grid_voltage": context.get("grid_voltage"),
                "has_gateway": context.get("has_gateway"),
                "gateway_connected": context.get("gateway_connected"),
                "state_of_energy": context.get("state_of_energy_bucket"),
                "active_overcurrent": context.get("active_overcurrent"),
                "active_overcurrent_standby": context.get(
                    "active_overcurrent_standby"
                ),
                "active_outage": context.get("active_outage"),
                "wifi_ssid": context.get("wifi_ssid"),
                "wifi_state": context.get("wifi_state"),
            },
            "usage": usage,
            "location": {
                "service_location_id": location.service_location_id,
                "address_id": location.address_id,
                "address_display": location.address_display,
            },
            "v2": {
                "snapshot": self._snapshot,
                "overview": self._overview,
                "billing_cycle": self._billing_cycle,
                "grid_support_active": (
                    grid_support_active(
                        self._overview["grid_support"], int(time.time())
                    )
                    if self._overview.get("available")
                    else None
                ),
                "health": self._v2_health,
            },
        }

    async def _async_fill_voltage(
        self, context: dict[str, Any], location: ServiceLocation
    ) -> None:
        """Base often omits gridVoltage from ServiceContext. Fill it in.

        Older voltage series first, then the v2 one. Best-effort: this never
        masks the primary payload.
        """
        if context.get("grid_voltage") is not None:
            context["grid_voltage_source"] = "context"
            context["grid_voltage_epoch"] = int(time.time())
            return

        grid: dict[str, Any] | None = None
        try:
            grid = await self.client.get_grid_status(location.service_location_id)
        except BasePowerError as exc:
            _LOGGER.debug("grid_status fetch failed: %s", exc)
        if grid and grid.get("grid_voltage") is not None:
            context["grid_voltage"] = grid["grid_voltage"]
            context["grid_voltage_ts"] = grid.get("latest_grid_voltage_ts")
            context["grid_voltage_epoch"] = grid.get("latest_grid_voltage_epoch")
            context["grid_voltage_source"] = "legacy"
            return

        now = _monotonic()
        if (
            self._last_v2_voltage_fetch is not None
            and now - self._last_v2_voltage_fetch
            < V2_VOLTAGE_MIN_INTERVAL.total_seconds()
        ):
            return
        self._last_v2_voltage_fetch = now
        result = await self._async_v2_call(
            "voltage",
            lambda: self.client.get_recent_grid_voltage(location.address_id),
        )
        if result and result.get("grid_voltage") is not None:
            context["grid_voltage"] = result["grid_voltage"]
            context["grid_voltage_epoch"] = result.get("latest_grid_voltage_epoch")
            context["grid_voltage_source"] = "v2"

    def _track_low_voltage(self, context: dict[str, Any]) -> None:
        """Count consecutive polls with a recent reading under the collapse line.

        An undated sample doesn't count: an old 0 V sample left in the series
        from an earlier outage must not read as a new one.
        """
        voltage = context.get("grid_voltage")
        epoch = context.get("grid_voltage_epoch")
        recent = (
            epoch is not None and time.time() - epoch <= _VOLTAGE_MAX_AGE_SECONDS
        )
        if voltage is not None and voltage < GRID_COLLAPSED_VOLTS and recent:
            self._low_voltage_polls += 1
        else:
            self._low_voltage_polls = 0

    async def _async_refresh_snapshot(
        self, context: dict[str, Any], location: ServiceLocation
    ) -> bool:
        """Refresh the battery snapshot when due. True if fetched this poll.

        A failed call clears the state: keeping an old "outage" would hold the
        home in outage after the grid returns.
        """
        suspect = (
            not context["available"]
            or bool(context.get("active_outage"))
            or self._low_voltage_polls > 0
            or self._snapshot.get("state") in OFF_GRID_STATES
        )
        interval = V2_SNAPSHOT_INTERVAL_SUSPECT if suspect else V2_SNAPSHOT_INTERVAL
        now = _monotonic()
        if (
            self._last_snapshot_fetch is not None
            and now - self._last_snapshot_fetch < interval.total_seconds() - 0.5
        ):
            return False
        self._last_snapshot_fetch = now
        result = await self._async_v2_call(
            "snapshot",
            lambda: self.client.get_battery_snapshot(location.address_id),
        )
        self._snapshot = result if result is not None else empty_snapshot()
        return result is not None

    async def _async_refresh_slow_v2(self, location: ServiceLocation) -> None:
        """Refresh the overview and billing cycle. A failure keeps the old data."""
        now = _monotonic()
        if (
            self._last_overview_fetch is None
            or now - self._last_overview_fetch
            >= V2_OVERVIEW_INTERVAL.total_seconds()
        ):
            self._last_overview_fetch = now
            start = dt_util.start_of_local_day()
            end = dt_util.start_of_local_day(start + timedelta(hours=25))
            overview = await self._async_v2_call(
                "overview",
                lambda: self.client.get_daily_overview(
                    location.address_id,
                    int(start.timestamp()),
                    int(end.timestamp()),
                ),
            )
            if overview and overview.get("available"):
                self._overview = overview
        if (
            self._last_billing_fetch is None
            or now - self._last_billing_fetch >= V2_BILLING_INTERVAL.total_seconds()
        ):
            self._last_billing_fetch = now
            cycle = await self._async_v2_call(
                "billing_cycle",
                lambda: self.client.get_current_billing_cycle(location.address_id),
            )
            if cycle and cycle.get("available"):
                self._billing_cycle = cycle

    async def _async_v2_call(
        self, name: str, call: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any] | None:
        """Run a v2 call. Any failure returns None and is recorded for diagnostics.

        This catches everything on purpose. The field layouts come from
        reverse engineering, so a parser surprise must not take down the poll,
        and a v2 auth error must not open a re-auth prompt while the older API
        is working.
        """
        health = self._v2_health.setdefault(
            name, {"last_success": None, "last_error": None}
        )
        try:
            result = await call()
        except Exception as exc:  # noqa: BLE001
            health["last_error"] = f"{type(exc).__name__}: {exc}"
            _LOGGER.debug("v2 %s fetch failed: %s", name, exc)
            return None
        health["last_success"] = dt_util.utcnow().isoformat()
        health["last_error"] = None
        return result

    def _maybe_fire_outage_event(self, outage: bool | None) -> None:
        """Fire on a real transition. Unknown is neither started nor ended."""
        if outage is None:
            return
        prev = self._last_outage_state
        self._last_outage_state = outage
        if prev is None:
            return
        if outage and not prev:
            self.hass.bus.async_fire(
                EVENT_OUTAGE_STARTED,
                {"entry_id": self.entry.entry_id},
            )
        elif not outage and prev:
            self.hass.bus.async_fire(
                EVENT_OUTAGE_ENDED,
                {"entry_id": self.entry.entry_id},
            )

    def _apply_adaptive_interval(self, active_outage: bool) -> None:
        # The tick interval must be the minimum of the "status" pace (grid or
        # outage) and the usage pace, so each endpoint gets polled at (or
        # faster than) its configured rate.
        status_interval = (
            self._outage_interval if active_outage else self._grid_interval
        )
        wanted = min(status_interval, self._usage_interval)
        if self.update_interval != wanted:
            self.update_interval = wanted
            _LOGGER.debug(
                "Switched poll interval to %s (on_battery=%s)",
                wanted,
                active_outage,
            )


def _update_failed(exc: BasePowerError) -> UpdateFailed:
    if isinstance(exc, BasePowerConnectionError):
        return UpdateFailed(f"Connection error: {exc}")
    if isinstance(exc, BasePowerProtocolError):
        return UpdateFailed(f"Protocol error: {exc}")
    return UpdateFailed(str(exc))


def _apply_snapshot_fallback(
    context: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    """Fill service-context readings from the v2 snapshot.

    The snapshot reports battery percent only while off grid, so Battery State
    of Energy is unavailable in this mode while the grid is up.
    """
    context["source"] = "v2_snapshot"
    flow = snapshot.get("power_flow") or {}

    def _watts(key: str) -> float | None:
        value = flow.get(key)
        return None if value is None else value * 1000.0

    context["home_power_w"] = _watts("to_home_kw")
    context["power_from_grid_w"] = _watts("from_grid_kw")
    context["power_from_storage_w"] = _watts("from_storage_kw")
    context["power_from_solar_w"] = _watts("from_solar_kw")
    context["backup_runtime_hours"] = snapshot.get("backup_hours")
    context["backup_runtime_at_750w_hours"] = snapshot.get("backup_hours_750w")
    context["state_of_energy_pct"] = snapshot.get("state_of_energy_percent")
    context["overcurrent_limit"] = snapshot.get("overcurrent_limit_kw")
    state = snapshot.get("state")
    context["active_overcurrent"] = state == "overcurrent"
    context["active_overcurrent_standby"] = state == "overcurrent_standby"
