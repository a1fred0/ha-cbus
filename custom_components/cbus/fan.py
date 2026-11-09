"""C-Bus fan controllers (sweep fans) as fans.

Untested on real hardware. A fan controller channel is controlled through the
lighting application: off, low, medium and high are levels 0, 86, 170 and 255.
"""
from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import FAN_LEVELS, TYPE_FAN
from .entity import CBusGroupEntity, setup_group_platform

SPEEDS = len(FAN_LEVELS) - 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_group_platform(hass, entry.runtime_data, async_add_entities, (TYPE_FAN,), CBusFan)


def level_to_speed(level: int) -> int:
    """Nearest speed (0 = off .. 3 = high) for any level."""
    return min(range(len(FAN_LEVELS)), key=lambda i: abs(FAN_LEVELS[i] - level))


def percentage_to_level(percentage: int) -> int:
    speed = math.ceil(max(0, min(100, percentage)) * SPEEDS / 100)
    return FAN_LEVELS[speed]


class CBusFan(CBusGroupEntity, FanEntity):
    """A C-Bus fan controller channel."""

    _attr_speed_count = SPEEDS
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    @property
    def percentage(self) -> int | None:
        level = self.level
        if level is None:
            return None
        return round(level_to_speed(level) * 100 / SPEEDS)

    @property
    def is_on(self) -> bool | None:
        level = self.level
        return None if level is None else level > 0

    async def async_set_percentage(self, percentage: int) -> None:
        await self._hub.async_set_level(self._group, percentage_to_level(percentage))

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        level = FAN_LEVELS[-1] if percentage is None else percentage_to_level(percentage)
        await self._hub.async_set_level(self._group, level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_set_level(self._group, 0)
