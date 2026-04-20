"""DataUpdateCoordinator for Base Power.

Polls `GetServiceStatus` (cheap, fast) every ``poll_interval_grid`` seconds,
dropping to ``poll_interval_outage`` seconds whenever ``activeOutage`` is True.

Pulls `MobileGetRecentUsage` (heavier, time-series) less frequently on a
secondary cadence to feed power/energy sensors without hammering the API.

On ``activeOutage`` transitions we fire ``base_power_outage_started`` /
``base_power_outage_ended`` on the HA event bus so automations can react.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    BasePowerAuthError,
    BasePowerClient,
    BasePowerConnectionError,
    BasePowerError,
    BasePowerProtocolError,
    ServiceLocation,
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
    DOMAIN,
    EVENT_OUTAGE_ENDED,
    EVENT_OUTAGE_STARTED,
)

_LOGGER = logging.getLogger(__name__)


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
        # MobileGetRecentUsage, which is what the Home Power sensor reads from.
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
        try:
            location = await self._ensure_auth_and_location()
            status = await self.client.get_service_status(
                location.service_location_id
            )
        except BasePowerAuthError as exc:
            # Clerk session expired/revoked - trigger reauth flow in the UI.
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except BasePowerConnectionError as exc:
            raise UpdateFailed(f"Connection error: {exc}") from exc
        except BasePowerProtocolError as exc:
            raise UpdateFailed(f"Protocol error: {exc}") from exc
        except BasePowerError as exc:  # noqa: BLE001
            raise UpdateFailed(str(exc)) from exc

        usage = self._last_usage
        now = time.monotonic()
        usage_interval_s = self._usage_interval.total_seconds()
        # Small tolerance prevents drift-induced skip when the main tick and
        # the usage interval are the same value.
        if now - self._last_usage_fetch >= usage_interval_s - 0.5:
            try:
                usage = await self.client.get_recent_usage(location.address_id)
                self._last_usage = usage
                self._last_usage_fetch = now
            except BasePowerError as exc:
                _LOGGER.debug("recent_usage fetch failed: %s", exc)

        self._maybe_fire_outage_event(status["active_outage"])
        self._apply_adaptive_interval(status["active_outage"])

        return {
            "status": status,
            "usage": usage,
            "location": {
                "service_location_id": location.service_location_id,
                "address_id": location.address_id,
                "address_display": location.address_display,
            },
        }

    def _maybe_fire_outage_event(self, active_outage: bool) -> None:
        prev = self._last_outage_state
        self._last_outage_state = active_outage
        if prev is None:
            return
        if active_outage and not prev:
            self.hass.bus.async_fire(
                EVENT_OUTAGE_STARTED,
                {"entry_id": self.entry.entry_id},
            )
        elif not active_outage and prev:
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
