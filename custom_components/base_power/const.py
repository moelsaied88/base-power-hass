"""Constants for the Base Power integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "base_power"

CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_SERVICE_LOCATION_ID: Final = "service_location_id"
CONF_ADDRESS_ID: Final = "address_id"

CONF_POLL_INTERVAL_GRID: Final = "poll_interval_grid"
CONF_POLL_INTERVAL_OUTAGE: Final = "poll_interval_outage"

DEFAULT_POLL_INTERVAL_GRID: Final = timedelta(seconds=30)
DEFAULT_POLL_INTERVAL_OUTAGE: Final = timedelta(seconds=5)

MIN_POLL_INTERVAL: Final = timedelta(seconds=5)
MAX_POLL_INTERVAL: Final = timedelta(minutes=10)

MANUFACTURER: Final = "Base Power Company"
MODEL: Final = "Base Battery"

EVENT_OUTAGE_STARTED: Final = f"{DOMAIN}_outage_started"
EVENT_OUTAGE_ENDED: Final = f"{DOMAIN}_outage_ended"
