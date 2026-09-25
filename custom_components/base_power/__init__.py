"""The Base Power integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import CONF_CODE, DOMAIN, SERVICE_SUBMIT_CODE
from .coordinator import BasePowerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SUBMIT_CODE_SCHEMA = vol.Schema(
    {vol.Required(CONF_CODE): vol.All(cv.string, vol.Strip, vol.Match(r"^\d{6}$"))}
)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide ``submit_code`` service.

    Lets an external tool (n8n, an IMAP automation, ...) answer a pending
    reauth flow with the emailed code, since HA's REST API can't list flows.
    """

    async def _async_submit_code(call: ServiceCall) -> None:
        flows = hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, match_context={"source": SOURCE_REAUTH}
        )
        if not flows:
            raise ServiceValidationError(
                "No Base Power re-authentication is waiting for a code"
            )

        code = call.data[CONF_CODE]
        failures: list[str] = []
        for flow in flows:
            result = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {CONF_CODE: code}
            )
            if (
                result["type"] is FlowResultType.ABORT
                and result.get("reason") == "reauth_successful"
            ):
                _LOGGER.info("Base Power re-authenticated via submit_code")
                return
            errors = result.get("errors") or {}
            failures.append(errors.get("base") or str(result.get("reason")))

        raise HomeAssistantError(
            f"Base Power did not accept the code: {', '.join(failures)}"
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SUBMIT_CODE, _async_submit_code, schema=SUBMIT_CODE_SCHEMA
    )
    return True


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


_POWER_SENSOR_KEYS = (
    "home_power",
    "power_from_grid",
    "power_from_storage",
    "power_to_battery",
    "power_from_battery_discharge",
    "power_from_solar",
    "home_from_grid",
    "home_from_battery",
    "home_from_solar",
)


def _reset_power_display_units(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """v0.7.1: pin W as the display unit for all power sensors.

    HA auto-promotes power sensors to ``kW`` at display time whenever the
    live value exceeds 1000 W. Integration helpers capture the source's
    displayed unit at creation time, so a helper built while the source
    was showing ``kW`` plus ``unit_prefix=k`` yields the bogus unit
    ``kkWh`` and the Energy Dashboard flags it as "unexpected device
    class".

    Starting in v0.7.1 the sensor descriptions declare
    ``suggested_unit_of_measurement=W`` which handles new installs, but
    ``suggested_unit_of_measurement`` is only applied the first time an
    entity is registered. Existing entities created on v0.6.x / v0.7.0
    need an explicit unit override to take effect. Force
    ``options.sensor.unit_of_measurement=W`` on every power sensor - it
    short-circuits the auto-promotion path so the display unit stays
    ``W`` regardless of magnitude.
    """

    reg = er.async_get(hass)
    sl_id = entry.data.get("service_location_id", "unknown")
    for key in _POWER_SENSOR_KEYS:
        uid = f"base_power_{sl_id}_{key}"
        entity_id = reg.async_get_entity_id("sensor", DOMAIN, uid)
        if not entity_id:
            continue
        entry_ = reg.async_get(entity_id)
        if not entry_:
            continue
        sensor_options = dict((entry_.options or {}).get("sensor", {}))
        if sensor_options.get("unit_of_measurement") == "W":
            continue
        sensor_options["unit_of_measurement"] = "W"
        _LOGGER.info("Pinning %s display unit to W", entity_id)
        reg.async_update_entity_options(entity_id, "sensor", sensor_options)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Base Power from a config entry."""

    _migrate_entity_ids(hass, entry)
    _reset_power_display_units(hass, entry)

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
