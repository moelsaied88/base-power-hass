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
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL_GRID,
    CONF_POLL_INTERVAL_OUTAGE,
    CONF_SERVICE_LOCATION_ID,
    DEFAULT_POLL_INTERVAL_GRID,
    DEFAULT_POLL_INTERVAL_OUTAGE,
    DOMAIN,
    EVENT_OUTAGE_ENDED,
    EVENT_OUTAGE_STARTED,
)

_LOGGER = logging.getLogger(__name__)

# Usage/energy data rarely changes faster than once per minute, so we cap the
# MobileGetRecentUsage call rate regardless of primary polling interval.
USAGE_MIN_INTERVAL_SECONDS = 60.0


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

        super().__init__(
            hass,
            _LOGGER,
            name=f"Base Power ({entry.data.get(CONF_EMAIL, 'unknown')})",
            update_interval=self._grid_interval,
        )

        session = aiohttp_client.async_get_clientsession(hass)
        self.client = BasePowerClient(
            session=session,
            email=entry.data[CONF_EMAIL],
            password=entry.data[CONF_PASSWORD],
        )
        self._signed_in = False
        self._location: ServiceLocation | None = None
        self._last_usage_fetch: float = 0.0
        self._last_outage_state: bool | None = None
        self._last_usage: dict[str, Any] = {}

    async def _ensure_auth_and_location(self) -> ServiceLocation:
        if not self._signed_in:
            await self.client.sign_in()
            self._signed_in = True
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
            self._signed_in = False
            raise UpdateFailed(f"Authentication failed: {exc}") from exc
        except BasePowerConnectionError as exc:
            raise UpdateFailed(f"Connection error: {exc}") from exc
        except BasePowerProtocolError as exc:
            raise UpdateFailed(f"Protocol error: {exc}") from exc
        except BasePowerError as exc:  # noqa: BLE001
            raise UpdateFailed(str(exc)) from exc

        usage = self._last_usage
        now = time.monotonic()
        if now - self._last_usage_fetch >= USAGE_MIN_INTERVAL_SECONDS:
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
        wanted = self._outage_interval if active_outage else self._grid_interval
        if self.update_interval != wanted:
            self.update_interval = wanted
            _LOGGER.debug(
                "Switched poll interval to %s (on_battery=%s)",
                wanted,
                active_outage,
            )
