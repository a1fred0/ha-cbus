"""C-Bus Lighting: native Home Assistant integration for Clipsal C-Bus."""
from __future__ import annotations

import asyncio
import codecs
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN
from .hub import CBusHub
from .usb import usb_reset

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.EVENT,
    Platform.FAN,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type CBusConfigEntry = ConfigEntry[CBusHub]


async def async_setup_entry(hass: HomeAssistant, entry: CBusConfigEntry) -> bool:
    """Set up C-Bus from a config entry."""
    hub = CBusHub(hass, entry)
    await hub.async_setup()
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _register_services(hass)
    return True


def _hub(hass: HomeAssistant) -> CBusHub:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise HomeAssistantError("C-Bus is not set up")


def _register_services(hass: HomeAssistant) -> None:
    """Diagnostic services for talking to the interface directly."""
    if hass.services.has_service(DOMAIN, "reconnect"):
        return

    async def reconnect(call: ServiceCall) -> None:
        hub = _hub(hass)
        await hub.pci.stop()
        await hub.pci.start()

    async def raw_open(call: ServiceCall) -> None:
        await _hub(hass).pci.open_raw()

    async def raw_send(call: ServiceCall) -> None:
        text: str = call.data["data"]
        data = codecs.decode(text, "unicode_escape").encode("latin-1")
        _LOGGER.info("Raw send %r", data)
        _hub(hass).pci.write_raw(data)

    async def raw_lines(call: ServiceCall) -> None:
        _hub(hass).pci.set_lines(call.data["dtr"], call.data["rts"])

    async def raw_baud(call: ServiceCall) -> None:
        _hub(hass).pci.set_baudrate(call.data["baud"])

    async def remove_group(call: ServiceCall) -> None:
        """Forget a group address; it is not re-added automatically."""
        hub = _hub(hass)
        group: int = call.data["group"]
        if group not in hub.groups and group in hub.ignored:
            raise HomeAssistantError(f"C-Bus group {group} is already removed")
        hub.async_remove_group(group)

    async def add_group(call: ServiceCall) -> None:
        """Add a group by hand (also undoes remove_group)."""
        _hub(hass).async_add_group(call.data["group"])

    async def trigger(call: ServiceCall) -> None:
        """Send a Trigger Control (scene) command."""
        await _hub(hass).async_trigger(call.data["group"], call.data["action_selector"])

    async def usb_reset_service(call: ServiceCall) -> None:
        hub = _hub(hass)
        method = call.data.get("method", "ioctl")
        await hub.pci.stop()
        try:
            result = await hass.async_add_executor_job(usb_reset, hub.port, method)
            _LOGGER.warning("USB reset: %s", result)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("USB reset (%s) failed: %r", method, err)
        await asyncio.sleep(call.data.get("delay", 5))
        await hub.pci.start()

    hass.services.async_register(
        DOMAIN,
        "usb_reset",
        usb_reset_service,
        schema=vol.Schema(
            {
                vol.Optional("method", default="ioctl"): vol.In(["ioctl", "authorized"]),
                vol.Optional("delay", default=5): vol.Coerce(float),
            }
        ),
    )
    group_schema = vol.Schema(
        {vol.Required("group"): vol.All(vol.Coerce(int), vol.Range(0, 254))}
    )
    hass.services.async_register(DOMAIN, "remove_group", remove_group, schema=group_schema)
    hass.services.async_register(DOMAIN, "add_group", add_group, schema=group_schema)
    hass.services.async_register(
        DOMAIN,
        "trigger",
        trigger,
        schema=vol.Schema(
            {
                vol.Required("group"): vol.All(vol.Coerce(int), vol.Range(0, 254)),
                vol.Required("action_selector"): vol.All(
                    vol.Coerce(int), vol.Range(0, 255)
                ),
            }
        ),
    )
    hass.services.async_register(DOMAIN, "reconnect", reconnect)
    hass.services.async_register(DOMAIN, "raw_open", raw_open)
    hass.services.async_register(
        DOMAIN, "raw_send", raw_send, schema=vol.Schema({vol.Required("data"): cv.string})
    )
    hass.services.async_register(
        DOMAIN,
        "raw_lines",
        raw_lines,
        schema=vol.Schema({vol.Required("dtr"): cv.boolean, vol.Required("rts"): cv.boolean}),
    )
    hass.services.async_register(
        DOMAIN,
        "raw_baud",
        raw_baud,
        schema=vol.Schema({vol.Required("baud"): vol.Coerce(int)}),
    )


async def async_unload_entry(hass: HomeAssistant, entry: CBusConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: CBusConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
