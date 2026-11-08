"""Button to re-read the state of the C-Bus network."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .hub import CBusHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CBusResyncButton(entry.runtime_data)])


class CBusResyncButton(ButtonEntity):
    """Re-read the level of every group from the network."""

    _attr_has_entity_name = True
    _attr_translation_key = "resync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: CBusHub) -> None:
        self._hub = hub
        self._attr_unique_id = f"{hub.uid}_resync"
        self._attr_device_info = DeviceInfo(identifiers={hub.hub_identifier})

    async def async_press(self) -> None:
        if not await self._hub.async_resync():
            raise HomeAssistantError("C-Bus resync failed (see log)")
