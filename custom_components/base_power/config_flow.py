"""Config + options flow for the Base Power integration.

Sign-in is a two-step passwordless flow:

1. User enters their email. We call Clerk's ``sign_ins`` + ``prepare_first_factor``
   endpoints, which causes Base Power to email a 6-digit code to the user.
2. User enters the code. We exchange it for a Clerk ``session_id``, fetch the
   first service location, and create the config entry with:

   * ``email`` (for display + reauth prefill)
   * ``session_id`` + ``client_id`` (long-lived Clerk credentials)
   * ``address_id`` + ``service_location_id``

No passwords or OTP codes are persisted.
"""

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
from homeassistant.const import CONF_EMAIL
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client

from .api import (
    BasePowerAuthError,
    BasePowerClient,
    BasePowerConnectionError,
    BasePowerError,
    _ClerkAuth,
)
from .const import (
    CONF_ADDRESS_ID,
    CONF_CLIENT_ID,
    CONF_CODE,
    CONF_POLL_INTERVAL_GRID,
    CONF_POLL_INTERVAL_OUTAGE,
    CONF_POLL_INTERVAL_USAGE,
    CONF_SERVICE_LOCATION_ID,
    CONF_SESSION_ID,
    DEFAULT_POLL_INTERVAL_GRID,
    DEFAULT_POLL_INTERVAL_OUTAGE,
    DEFAULT_POLL_INTERVAL_USAGE,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


EMAIL_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})


class BasePowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """UI config flow for Base Power."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._auth: _ClerkAuth | None = None
        self._reauth_entry: ConfigEntry | None = None

    # ---- Initial setup (step 1: email, step 2: code) ---------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            session = aiohttp_client.async_get_clientsession(self.hass)
            auth = _ClerkAuth(session, email=email)

            try:
                await auth.start_sign_in()
            except BasePowerAuthError as exc:
                _LOGGER.debug("sign-in start failed: %s", exc)
                errors["base"] = "invalid_auth"
            except BasePowerConnectionError:
                errors["base"] = "cannot_connect"
            except BasePowerError as exc:
                _LOGGER.exception("unexpected error starting sign-in: %s", exc)
                errors["base"] = "unknown"
            else:
                self._email = email
                self._auth = auth
                return await self.async_step_code()

        return self.async_show_form(
            step_id="user",
            data_schema=EMAIL_SCHEMA,
            errors=errors,
        )

    async def async_step_code(
        self,
        user_input: dict[str, Any] | None = None,
        *,
        step_id: str = "code",
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._auth is not None
        assert self._email is not None

        if user_input is not None:
            code = user_input[CONF_CODE].strip()
            session = aiohttp_client.async_get_clientsession(self.hass)
            client = BasePowerClient(session, auth=self._auth)

            try:
                await self._auth.attempt_sign_in(code)
                locations = await client.get_available_locations()
                if not locations:
                    errors["base"] = "no_locations"
                else:
                    first = locations[0]
                    service_location = await client.resolve_service_location(
                        first.address_id
                    )
            except BasePowerAuthError as exc:
                _LOGGER.debug("code verification failed: %s", exc)
                errors["base"] = "invalid_code"
            except BasePowerConnectionError:
                errors["base"] = "cannot_connect"
            except BasePowerError as exc:
                _LOGGER.exception("unexpected error verifying code: %s", exc)
                errors["base"] = "unknown"
            else:
                unique_id = (
                    f"{self._email.lower()}::"
                    f"{service_location.service_location_id}"
                )

                if self._reauth_entry is not None:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={
                            **self._reauth_entry.data,
                            CONF_EMAIL: self._email,
                            CONF_SESSION_ID: self._auth.session_id,
                            CONF_CLIENT_ID: self._auth.client_id,
                        },
                    )
                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")

                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=service_location.address_display or self._email,
                    data={
                        CONF_EMAIL: self._email,
                        CONF_SESSION_ID: self._auth.session_id,
                        CONF_CLIENT_ID: self._auth.client_id,
                        CONF_ADDRESS_ID: service_location.address_id,
                        CONF_SERVICE_LOCATION_ID: (
                            service_location.service_location_id
                        ),
                    },
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=CODE_SCHEMA,
            description_placeholders={"email": self._email or ""},
            errors=errors,
        )

    # ---- Reauth (Clerk session expired / revoked) ------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._email = entry_data.get(CONF_EMAIL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            session = aiohttp_client.async_get_clientsession(self.hass)
            self._auth = _ClerkAuth(session, email=self._email)
            try:
                await self._auth.start_sign_in()
            except BasePowerAuthError:
                errors["base"] = "invalid_auth"
            except BasePowerConnectionError:
                errors["base"] = "cannot_connect"
            except BasePowerError:
                errors["base"] = "unknown"
            if errors:
                return self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=CODE_SCHEMA,
                    description_placeholders={"email": self._email or ""},
                    errors=errors,
                )
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=CODE_SCHEMA,
                description_placeholders={"email": self._email or ""},
            )

        return await self.async_step_code(user_input, step_id="reauth_confirm")

    # ---- Options flow ----------------------------------------------------

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
                vol.Optional(
                    CONF_POLL_INTERVAL_USAGE,
                    default=current.get(
                        CONF_POLL_INTERVAL_USAGE,
                        DEFAULT_POLL_INTERVAL_USAGE.total_seconds(),
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
