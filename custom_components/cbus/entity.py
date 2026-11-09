"""Shared base for entities that control a C-Bus lighting group address."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, LIGHTING_APPLICATION, signal_group, signal_new_group, signal_status
from .hub import CBusHub


def setup_group_platform(
    hass: HomeAssistant,
    hub: CBusHub,
    async_add_entities: AddConfigEntryEntitiesCallback,
    types: tuple[str, ...],
    factory: Callable[[CBusHub, int], Entity],
) -> None:
    """Add an entity for every group of the given types, now and later."""
    added: set[int] = set()

    @callback
    def _add(groups) -> None:
        new = [
            g
            for g in groups
            if g not in added and g in hub.groups and hub.group_type(g) in types
        ]
        added.update(new)
        entities = []
        for group in sorted(new):
            entity = factory(hub, group)
            # forget it when removed (eg by remove_group) so it can come back
            entity.async_on_remove(lambda g=group: added.discard(g))
            entities.append(entity)
        if entities:
            async_add_entities(entities)

    @callback
    def _new_group(group: int) -> None:
        _add([group])

    _add(list(hub.groups))
    hub.entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_group(hub.entry.entry_id), _new_group)
    )


class CBusGroupEntity(Entity):
    """An entity for one C-Bus group address (application 56)."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

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
    def level(self) -> int | None:
        """Current level of the group (0-255), if known."""
        return self._hub.levels.get(self._group)
