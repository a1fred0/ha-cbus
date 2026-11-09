"""Events from wall switches (key units) and Trigger Control (scenes)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import (
    DOMAIN,
    TRIGGER_EVENT_TYPES,
    UNIT_EVENT_TYPES,
    signal_new_trigger,
    signal_new_unit,
    signal_trigger,
    signal_unit,
)
from .hub import CBusHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data

    async_add_entities(CBusUnitEvent(hub, unit) for unit in sorted(hub.units))
    async_add_entities(CBusTriggerEvent(hub, group) for group in sorted(hub.triggers))

    @callback
    def _new_unit(unit: int) -> None:
        async_add_entities([CBusUnitEvent(hub, unit)])

    @callback
    def _new_trigger(group: int) -> None:
        async_add_entities([CBusTriggerEvent(hub, group)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_unit(entry.entry_id), _new_unit)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_trigger(entry.entry_id), _new_trigger)
    )


class CBusUnitEvent(EventEntity):
    """Presses on a wall switch (or commands from any other C-Bus unit).

    C-Bus switches send ordinary lighting commands, so the event types are
    built from them: "on", "off", "ramp" (dimming, or a set level),
    "terminate" (dimming stopped), and when one press sends several commands
    at once, "all_off", "all_on" or "scene". The groups affected are in the
    event's attributes.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_event_types = UNIT_EVENT_TYPES
    _attr_translation_key = "unit"

    def __init__(self, hub: CBusHub, unit: int) -> None:
        self._hub = hub
        self._unit = unit
        info = hub.units.get(unit, {})
        self._attr_unique_id = f"{hub.uid}_unit_{unit}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=hub.unit_name(unit),
            manufacturer="Clipsal",
            model=info.get("model") or "C-Bus unit",
            serial_number=f"unit {unit}",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_unit(self._hub.entry.entry_id, self._unit),
                self._handle,
            )
        )

    @callback
    def _handle(self, event_type: str, data: dict[str, Any]) -> None:
        self._trigger_event(event_type, {"unit_address": self._unit, **data})
        self.async_write_ha_state()


class CBusTriggerEvent(EventEntity):
    """A Trigger Control group (C-Bus scene). Untested on real hardware."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_event_types = TRIGGER_EVENT_TYPES

    def __init__(self, hub: CBusHub, group: int) -> None:
        self._hub = hub
        self._group = group
        self._attr_unique_id = f"{hub.uid}_trigger_{group}"
        self._attr_name = hub.trigger_name(group)
        self._attr_device_info = DeviceInfo(identifiers={hub.hub_identifier})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_trigger(self._hub.entry.entry_id, self._group),
                self._handle,
            )
        )

    @callback
    def _handle(self, event_type: str, data: dict[str, Any]) -> None:
        self._trigger_event(event_type, {"trigger_group": self._group, **data})
        self.async_write_ha_state()
