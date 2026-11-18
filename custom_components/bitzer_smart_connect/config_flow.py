"""Config flow for Bitzer Smart Connect."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .api import BitzerAuth, BitzerClient, CannotConnect, InvalidAuth
from .api.models import Device
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_EXPOSE_ALL,
    CONF_FAN_MAX,
    CONF_FAN_MIN,
    CONF_TEMP_MAX,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    DEFAULT_EXPOSE_ALL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import BitzerConfigEntry

USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


class BitzerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._creds: dict[str, str] = {}
        self._auth: BitzerAuth | None = None
        self._devices: list[Device] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            auth = BitzerAuth(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            try:
                await auth.async_login()
                self._devices = await BitzerClient(auth).async_get_user_devices()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self._creds = dict(user_input)
                self._auth = auth
                if len(self._devices) == 1:
                    return await self.async_step_device({CONF_DEVICE_ID: self._devices[0].id})
                return await self.async_step_device()
            await auth.async_close()
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a device from the discovered list, or enter an id if discovery came back empty."""
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = int(user_input[CONF_DEVICE_ID])
            name = user_input.get(CONF_DEVICE_NAME) or next(
                (d.name for d in self._devices if d.id == device_id), f"Device {device_id}"
            )
            try:
                config = await BitzerClient(self._auth).async_get_config(device_id)
            except CannotConnect:
                config = None
            if config and config.parameters:
                return await self._create_entry(device_id, name)
            errors["base"] = "device_unreadable"

        if self._devices:
            schema = vol.Schema(
                {vol.Required(CONF_DEVICE_ID): vol.In({str(d.id): d.name for d in self._devices})}
            )
        else:  # discovery empty — let the user type the device id from the web app URL
            schema = vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): cv.positive_int,
                    vol.Optional(CONF_DEVICE_NAME): str,
                }
            )
        return self.async_show_form(step_id="device", data_schema=schema, errors=errors)

    async def _create_entry(self, device_id: int, name: str) -> ConfigFlowResult:
        if self._auth is not None:
            await self._auth.async_close()
        await self.async_set_unique_id(str(device_id))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=name,
            data={**self._creds, CONF_DEVICE_ID: device_id, CONF_DEVICE_NAME: name},
        )

    # -- reauth -----------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            auth = BitzerAuth(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            try:
                await auth.async_login()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=user_input
                )
            finally:
                await auth.async_close()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: BitzerConfigEntry) -> "BitzerOptionsFlow":
        return BitzerOptionsFlow()


class BitzerOptionsFlow(OptionsFlow):
    """Options: reconcile poll interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options

        def _override(key: str):  # optional, pre-filled from current value, blank clears it
            return vol.Optional(key, description={"suggested_value": opts.get(key)})

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
                vol.Optional(
                    CONF_EXPOSE_ALL,
                    default=opts.get(CONF_EXPOSE_ALL, DEFAULT_EXPOSE_ALL),
                ): bool,
                _override(CONF_TEMP_MIN): vol.Coerce(float),
                _override(CONF_TEMP_MAX): vol.Coerce(float),
                _override(CONF_TEMP_STEP): vol.Coerce(float),
                _override(CONF_FAN_MIN): vol.Coerce(int),
                _override(CONF_FAN_MAX): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
