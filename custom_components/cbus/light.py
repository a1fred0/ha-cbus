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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import TYPE_LIGHT, TYPE_LIGHT_ONOFF
from .entity import CBusGroupEntity, setup_group_platform
from .hub import CBusHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up lights for every known group, and any discovered later."""
    setup_group_platform(
        hass,
        entry.runtime_data,
        async_add_entities,
        (TYPE_LIGHT, TYPE_LIGHT_ONOFF),
        CBusLight,
    )


class CBusLight(CBusGroupEntity, LightEntity):
    """A C-Bus lighting group address."""

    def __init__(self, hub: CBusHub, group: int) -> None:
        super().__init__(hub, group)
        if hub.group_type(group) == TYPE_LIGHT_ONOFF:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._dimmable = False
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_supported_features = LightEntityFeature.TRANSITION
            self._dimmable = True

    @property
    def brightness(self) -> int | None:
        if not self._dimmable:
            return None
        return self.level

    @property
    def is_on(self) -> bool | None:
        level = self.level
        if level is None:
            return None
        return level > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not self._dimmable:
            await self._hub.async_set_level(self._group, 255)
            return
        await self._hub.async_set_level(
            self._group,
            kwargs.get(ATTR_BRIGHTNESS, 255),
            kwargs.get(ATTR_TRANSITION),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        transition = kwargs.get(ATTR_TRANSITION) if self._dimmable else None
        await self._hub.async_set_level(self._group, 0, transition)
