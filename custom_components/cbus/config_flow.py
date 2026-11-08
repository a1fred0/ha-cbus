"""Config flow for C-Bus Lighting."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from .const import (
    CONF_PORT,
    CONF_PROJECT_FILE,
    CONF_RESYNC_MINUTES,
    CONF_TIMESYNC,
    DEFAULT_RESYNC_MINUTES,
    DEFAULT_TIMESYNC,
    DOMAIN,
)

BY_ID_DIR = "/dev/serial/by-id"
TITLE = "C-Bus"


def serial_by_id(device: str) -> str:
    """Return the stable /dev/serial/by-id path for a device, if there is one."""
    by_id = Path(BY_ID_DIR)
    if not by_id.is_dir():
        return device
    try:
        real = os.path.realpath(device)
        for link in by_id.iterdir():
            if os.path.realpath(link) == real:
                return str(link)
    except OSError:
        pass
    return device


def list_ports() -> dict[str, str]:
    """Return {device path: description} for serial ports. Blocking."""
    from serial.tools import list_ports as lp  # pylint: disable=import-outside-toplevel

    ports: dict[str, str] = {}
    for port in lp.comports():
        path = serial_by_id(port.device)
        desc = port.description or port.device
        if port.serial_number:
            desc = f"{desc} ({port.serial_number})"
        ports[path] = desc
    return ports


def port_serial_number(device: str) -> str | None:
    """Serial number of a USB serial port, if known. Blocking."""
    from serial.tools import list_ports as lp  # pylint: disable=import-outside-toplevel

    real = os.path.realpath(device)
    for port in lp.comports():
        if os.path.realpath(port.device) == real:
            return port.serial_number
    return None


class CBusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for C-Bus."""

    VERSION = 1

    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> ConfigFlowResult:
        """Set up automatically when a C-Bus USB interface is found."""
        path = await self.hass.async_add_executor_job(
            serial_by_id, discovery_info.device
        )
        await self.async_set_unique_id(discovery_info.serial_number or path)
        self._abort_if_unique_id_configured(updates={CONF_PORT: path})
        # Nothing to choose: the interface is unambiguous, so create the entry
        # straight away.
        return self.async_create_entry(title=TITLE, data={CONF_PORT: path})

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the serial port manually."""
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input[CONF_PORT]
            exists = await self.hass.async_add_executor_job(os.path.exists, path)
            if not exists:
                errors[CONF_PORT] = "not_found"
            else:
                serial = await self.hass.async_add_executor_job(
                    port_serial_number, path
                )
                await self.async_set_unique_id(serial or path)
                self._abort_if_unique_id_configured(updates={CONF_PORT: path})
                return self.async_create_entry(title=TITLE, data={CONF_PORT: path})

        ports = await self.hass.async_add_executor_job(list_ports)
        default = next((p for p, d in ports.items() if "c-bus" in d.lower()), None)
        if ports:
            field = vol.In(ports)
        else:
            field = str
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PORT, default=default or next(iter(ports), "/dev/ttyUSB0")
                ): field
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CBusOptionsFlow()


class CBusOptionsFlow(OptionsFlow):
    """Options: project file, resync interval, time broadcasts."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            project = (user_input.get(CONF_PROJECT_FILE) or "").strip()
            if project:
                exists = await self.hass.async_add_executor_job(
                    os.path.isfile, project
                )
                if not exists:
                    errors[CONF_PROJECT_FILE] = "not_found"
            if not errors:
                user_input[CONF_PROJECT_FILE] = project
                return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PROJECT_FILE,
                    description={"suggested_value": options.get(CONF_PROJECT_FILE, "")},
                ): str,
                vol.Required(
                    CONF_RESYNC_MINUTES,
                    default=options.get(CONF_RESYNC_MINUTES, DEFAULT_RESYNC_MINUTES),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
                vol.Required(
                    CONF_TIMESYNC,
                    default=options.get(CONF_TIMESYNC, DEFAULT_TIMESYNC),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
