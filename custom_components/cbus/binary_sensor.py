"""Connection status of the C-Bus interface."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import signal_status
from .hub import CBusHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CBusConnectionSensor(entry.runtime_data)])


class CBusConnectionSensor(BinarySensorEntity):
    """Whether the C-Bus PC Interface is connected and responding."""

    _attr_has_entity_name = True
    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, hub: CBusHub) -> None:
        self._hub = hub
        self._attr_unique_id = f"{hub.uid}_connection"
        self._attr_device_info = DeviceInfo(identifiers={hub.hub_identifier})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_status(self._hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )

    @property
    def is_on(self) -> bool:
        return self._hub.pci.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hub = self._hub
        return {
            "port": hub.port,
            "groups": sorted(hub.groups),
            "last_sync": hub.last_sync.isoformat() if hub.last_sync else None,
            "last_sync_ok": hub.last_sync_ok,
            "sync_count": hub.sync_count,
            "level_requests_supported": hub.pci.level_requests_supported,
            "last_error": hub.pci.last_error,
        }
