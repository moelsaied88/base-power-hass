"""The Base Power integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import BasePowerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


def _migrate_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """One-time entity_id migrations for key/name changes across versions.

    v0.6.4: the signed ``power_from_storage`` sensor's friendly name moved
    from "Power From Battery" to "Battery Net Power", and a new
    positive-only ``power_from_battery_discharge`` sensor wants the
    ``sensor.base_power_power_from_battery`` slot. Rename the old entity
    out of the way so the new one gets the natural slug.
    """

    reg = er.async_get(hass)
    sl_id = entry.data.get("service_location_id", "unknown")
    old_uid = f"base_power_{sl_id}_power_from_storage"
    entity_id = reg.async_get_entity_id("sensor", DOMAIN, old_uid)
    if not entity_id:
        return
    if entity_id == "sensor.base_power_power_from_battery":
        target = "sensor.base_power_battery_net_power"
        if reg.async_get(target) is None:
            _LOGGER.info(
                "Renaming %s -> %s to free entity_id for new discharge sensor",
                entity_id,
                target,
            )
            reg.async_update_entity(entity_id, new_entity_id=target)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Base Power from a config entry."""

    _migrate_entity_ids(hass, entry)

    coordinator = BasePowerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
