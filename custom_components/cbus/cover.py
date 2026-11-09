"""C-Bus shutter relays (blinds, curtains, awnings) as covers.

Untested on real hardware. A shutter relay is controlled through the lighting
application: level 255 opens, 0 closes and 2% (level 5) stops, as a wall
plate's shutter function does. With "level translation" enabled on the relay,
0%, 3-97% and 100% move to an approximate position; 1%, 2%, 98% and 99% are
commands, so positions avoid them. The relay does not report movement, so the
position shown is the last one set.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import (
    COVER_CLOSED,
    COVER_COMMAND_LEVELS,
    COVER_OPEN,
    COVER_STOP,
    TYPE_COVER,
)
from .entity import CBusGroupEntity, setup_group_platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_group_platform(
        hass, entry.runtime_data, async_add_entities, (TYPE_COVER,), CBusCover
    )


def position_to_level(position: int) -> int:
    """0-100% -> 0-255, avoiding the levels the relay treats as commands."""
    position = max(0, min(100, int(position)))
    if position == 0:
        return COVER_CLOSED
    if position == 100:
        return COVER_OPEN
    position = max(3, min(97, position))
    return round(position * 255 / 100)


def level_to_position(level: int) -> int:
    return round(level * 100 / 255)


class CBusCover(CBusGroupEntity, CoverEntity):
    """A C-Bus shutter relay channel."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    @property
    def current_cover_position(self) -> int | None:
        level = self.level
        if level is None or level in COVER_COMMAND_LEVELS:
            return None
        return level_to_position(level)

    @property
    def is_closed(self) -> bool | None:
        level = self.level
        if level is None:
            return None
        return level == COVER_CLOSED

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(self._group, COVER_OPEN)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(self._group, COVER_CLOSED)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(self._group, COVER_STOP, update_state=False)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(
            self._group, position_to_level(kwargs[ATTR_POSITION])
        )
