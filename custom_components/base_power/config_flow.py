"""Config + options flow for the Base Power integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client

from .api import (
    BasePowerAuthError,
    BasePowerClient,
    BasePowerConnectionError,
    BasePowerError,
)
from .const import (
    CONF_ADDRESS_ID,
    CONF_POLL_INTERVAL_GRID,
    CONF_POLL_INTERVAL_OUTAGE,
    CONF_SERVICE_LOCATION_ID,
    DEFAULT_POLL_INTERVAL_GRID,
    DEFAULT_POLL_INTERVAL_OUTAGE,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class BasePowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """UI config flow for Base Power."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]

            session = aiohttp_client.async_get_clientsession(self.hass)
            client = BasePowerClient(session=session, email=email, password=password)

            try:
                await client.sign_in()
                locations = await client.get_available_locations()
                if not locations:
                    errors["base"] = "no_locations"
                else:
                    first = locations[0]
                    service_location = await client.resolve_service_location(
                        first.address_id
                    )
            except BasePowerAuthError:
                errors["base"] = "invalid_auth"
            except BasePowerConnectionError:
                errors["base"] = "cannot_connect"
            except BasePowerError as exc:
                _LOGGER.exception("unexpected error during config flow: %s", exc)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    f"{email.lower()}::{service_location.service_location_id}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=service_location.address_display or email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_ADDRESS_ID: service_location.address_id,
                        CONF_SERVICE_LOCATION_ID: service_location.service_location_id,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=CREDENTIALS_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return BasePowerOptionsFlow(entry)


class BasePowerOptionsFlow(OptionsFlow):
    """Options flow - just the two poll intervals."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_POLL_INTERVAL_GRID,
                    default=current.get(
                        CONF_POLL_INTERVAL_GRID,
                        DEFAULT_POLL_INTERVAL_GRID.total_seconds(),
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_POLL_INTERVAL.total_seconds(),
                        max=MAX_POLL_INTERVAL.total_seconds(),
                    ),
                ),
                vol.Optional(
                    CONF_POLL_INTERVAL_OUTAGE,
                    default=current.get(
                        CONF_POLL_INTERVAL_OUTAGE,
                        DEFAULT_POLL_INTERVAL_OUTAGE.total_seconds(),
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_POLL_INTERVAL.total_seconds(),
                        max=MAX_POLL_INTERVAL.total_seconds(),
                    ),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
