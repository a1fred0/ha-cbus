"""Config flow for C-Bus Lighting."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from .const import (
    CONF_GROUP_TYPES,
    CONF_PORT,
    CONF_PROJECT_FILE,
    CONF_PROJECT_UPLOAD,
    CONF_RESYNC_MINUTES,
    CONF_TIMESYNC,
    DEFAULT_RESYNC_MINUTES,
    DEFAULT_TIMESYNC,
    DOMAIN,
    GROUP_TYPES,
    TYPE_LIGHT,
)
from .project import load_project
from .protocol import is_network_port

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
            path = user_input[CONF_PORT].strip()
            if is_network_port(path):
                # An Ethernet interface (5500CN / CNI), eg tcp://192.168.1.50:10001
                await self.async_set_unique_id(path)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=TITLE, data={CONF_PORT: path})
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
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PORT, default=default or next(iter(ports), "/dev/ttyUSB0")
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": p, "label": d} for p, d in ports.items()],
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CBusOptionsFlow()


def save_uploaded_project(hass: HomeAssistant, file_id: str) -> str:
    """Copy an uploaded project file into the config folder. Blocking."""
    with process_uploaded_file(hass, file_id) as upload:
        load_project(str(upload))  # raises if it can't be read
        suffix = ".cbz" if upload.read_bytes()[:2] == b"PK" else ".xml"
        target = Path(hass.config.path(DOMAIN, f"project{suffix}"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upload, target)
    return str(target)


class CBusOptionsFlow(OptionsFlow):
    """Options: settings, and what each group controls."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "group_types"]
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)
        if user_input is not None:
            project = (user_input.get(CONF_PROJECT_FILE) or "").strip()
            if upload := user_input.pop(CONF_PROJECT_UPLOAD, None):
                try:
                    project = await self.hass.async_add_executor_job(
                        save_uploaded_project, self.hass, upload
                    )
                except Exception:  # noqa: BLE001
                    errors[CONF_PROJECT_UPLOAD] = "invalid_project"
            elif project:
                exists = await self.hass.async_add_executor_job(
                    os.path.isfile, project
                )
                if not exists:
                    errors[CONF_PROJECT_FILE] = "not_found"
            if not errors:
                options.update(user_input)
                options[CONF_PROJECT_FILE] = project
                return self.async_create_entry(data=options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_PROJECT_UPLOAD): FileSelector(
                    FileSelectorConfig(accept=".cbz,.xml")
                ),
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
        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )

    async def async_step_group_types(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what each group controls: light, relay, switch, blind or fan."""
        options = dict(self.config_entry.options)
        current: dict[str, str] = dict(options.get(CONF_GROUP_TYPES) or {})
        hub = getattr(self.config_entry, "runtime_data", None)
        groups: dict[int, str] = {}
        if hub is not None:
            groups = {ga: hub.group_name(ga) for ga in sorted(hub.groups)}
        for ga in current:
            groups.setdefault(int(ga), f"C-Bus Group {ga}")
        labels = {f"{ga}: {name}": ga for ga, name in sorted(groups.items())}

        if user_input is not None:
            types = {
                str(labels[label]): kind
                for label, kind in user_input.items()
                if label in labels and kind != TYPE_LIGHT
            }
            options[CONF_GROUP_TYPES] = types
            return self.async_create_entry(data=options)

        if not labels:
            return self.async_abort(reason="no_groups")
        selector = SelectSelector(
            SelectSelectorConfig(
                options=list(GROUP_TYPES),
                translation_key="group_type",
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        schema = vol.Schema(
            {
                vol.Required(label, default=current.get(str(ga), TYPE_LIGHT)): selector
                for label, ga in labels.items()
            }
        )
        return self.async_show_form(step_id="group_types", data_schema=schema)
