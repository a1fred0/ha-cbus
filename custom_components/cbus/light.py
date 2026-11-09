"""C-Bus lighting groups as Home Assistant lights."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import DOMAIN, LIGHTING_APPLICATION, signal_group, signal_new_group, signal_status
from .hub import CBusHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up lights for every known group, and any discovered later."""
    hub = entry.runtime_data
    added: set[int] = set()

    @callback
    def _add(groups: list[int]) -> None:
        new = [g for g in groups if g not in added]
        added.update(new)
        if new:
            async_add_entities(CBusLight(hub, g) for g in sorted(new))

    @callback
    def _new_group(group: int) -> None:
        _add([group])

    _add(list(hub.groups))
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_group(entry.entry_id), _new_group)
    )


class CBusLight(LightEntity):
    """A C-Bus lighting group address."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, hub: CBusHub, group: int) -> None:
        self._hub = hub
        self._group = group
        uid = hub.unique_id_for(group)
        self._attr_unique_id = uid
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            name=hub.group_name(group),
            manufacturer="Clipsal",
            model="C-Bus lighting group",
        )
        self._attr_extra_state_attributes = {
            "group_address": group,
            "application": LIGHTING_APPLICATION,
        }

    async def async_added_to_hass(self) -> None:
        entry_id = self._hub.entry.entry_id
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_group(entry_id, self._group), self.async_write_ha_state
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_status(entry_id), self.async_write_ha_state
            )
        )

    @property
    def available(self) -> bool:
        return self._hub.pci.connected

    @property
    def brightness(self) -> int | None:
        return self._hub.levels.get(self._group)

    @property
    def is_on(self) -> bool | None:
        level = self._hub.levels.get(self._group)
        if level is None:
            return None
        return level > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(
            self._group,
            kwargs.get(ATTR_BRIGHTNESS, 255),
            kwargs.get(ATTR_TRANSITION),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(self._group, 0, kwargs.get(ATTR_TRANSITION))
