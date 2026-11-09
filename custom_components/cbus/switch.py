"""C-Bus groups configured as switches (relays that aren't lights)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import TYPE_SWITCH
from .entity import CBusGroupEntity, setup_group_platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_group_platform(
        hass, entry.runtime_data, async_add_entities, (TYPE_SWITCH,), CBusSwitch
    )


class CBusSwitch(CBusGroupEntity, SwitchEntity):
    """A C-Bus group address as an on/off switch."""

    @property
    def is_on(self) -> bool | None:
        level = self.level
        return None if level is None else level > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(self._group, 255)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(self._group, 0)
