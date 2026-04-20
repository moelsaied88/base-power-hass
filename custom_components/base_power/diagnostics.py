"""Diagnostics for Base Power.

The diagnostics dump is intentionally conservative: credentials, JWTs, session
ids, and anything that could be used to hijack the user's Base account are
redacted even though HA's download_diagnostics UI asks the user to review the
dump before sharing.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS_ID,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SERVICE_LOCATION_ID,
    DOMAIN,
)
from .coordinator import BasePowerCoordinator

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_ADDRESS_ID,
    CONF_SERVICE_LOCATION_ID,
    "address_id",
    "address_display",
    "wifi_ssid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: BasePowerCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(entry.entry_id)
    )
    data = coordinator.data if coordinator else None
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "coordinator_data": async_redact_data(data or {}, TO_REDACT),
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds() if coordinator else None
        ),
    }
