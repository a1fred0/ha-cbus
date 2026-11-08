"""C-Bus Lighting: native Home Assistant integration for Clipsal C-Bus."""
from __future__ import annotations

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

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.BINARY_SENSOR, Platform.BUTTON]

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
