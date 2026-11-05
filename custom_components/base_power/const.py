"""Constants for the Base Power integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "base_power"

CONF_EMAIL: Final = "email"
CONF_CODE: Final = "code"
CONF_SESSION_ID: Final = "session_id"
CONF_CLIENT_ID: Final = "client_id"
CONF_SERVICE_LOCATION_ID: Final = "service_location_id"
CONF_ADDRESS_ID: Final = "address_id"

CONF_POLL_INTERVAL_GRID: Final = "poll_interval_grid"
CONF_POLL_INTERVAL_OUTAGE: Final = "poll_interval_outage"
CONF_POLL_INTERVAL_USAGE: Final = "poll_interval_usage"

DEFAULT_POLL_INTERVAL_GRID: Final = timedelta(seconds=5)
DEFAULT_POLL_INTERVAL_OUTAGE: Final = timedelta(seconds=5)
# MobileGetRecentUsage returns 15-minute energy buckets, so polling it faster
# than a minute doesn't help. It only feeds the "Energy From X (recent)"
# totals; live power/SoE come from MobileGetServiceContext on the grid tick.
DEFAULT_POLL_INTERVAL_USAGE: Final = timedelta(minutes=5)

MIN_POLL_INTERVAL: Final = timedelta(seconds=5)
MAX_POLL_INTERVAL: Final = timedelta(minutes=10)

# dashboard.mobile.v2 schedules. Fixed on purpose: these calls add data and act
# as a fallback, so they stay light and don't scale with the primary poll.
V2_SNAPSHOT_INTERVAL: Final = timedelta(minutes=10)
V2_SNAPSHOT_INTERVAL_SUSPECT: Final = timedelta(seconds=30)
V2_OVERVIEW_INTERVAL: Final = timedelta(minutes=15)
V2_BILLING_INTERVAL: Final = timedelta(hours=6)
V2_VOLTAGE_MIN_INTERVAL: Final = timedelta(seconds=30)

MANUFACTURER: Final = "Base Power Company"
MODEL: Final = "Base Battery"

SERVICE_SUBMIT_CODE: Final = "submit_code"

EVENT_OUTAGE_STARTED: Final = f"{DOMAIN}_outage_started"
EVENT_OUTAGE_ENDED: Final = f"{DOMAIN}_outage_ended"
