"""Temperature Broadcast and Measurement readings as sensors.

Untested on real hardware. Sensors appear the first time a reading is seen on
the network, and keep their last value across restarts.
"""
from __future__ import annotations

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CBusConfigEntry
from .const import signal_new_sensor, signal_sensor
from .hub import CBusHub

DEVICE_CLASSES = {
    "°C": SensorDeviceClass.TEMPERATURE,
    "A": SensorDeviceClass.CURRENT,
    "Hz": SensorDeviceClass.FREQUENCY,
    "kg": SensorDeviceClass.WEIGHT,
    "lx": SensorDeviceClass.ILLUMINANCE,
    "m": SensorDeviceClass.DISTANCE,
    "m/s": SensorDeviceClass.SPEED,
    "Pa": SensorDeviceClass.PRESSURE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(CBusSensor(hub, key) for key in sorted(hub.sensors))

    @callback
    def _new(key: str) -> None:
        async_add_entities([CBusSensor(hub, key)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_sensor(entry.entry_id), _new)
    )


class CBusSensor(RestoreSensor):
    """A C-Bus temperature or measurement channel."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: CBusHub, key: str) -> None:
        self._hub = hub
        self._key = key
        info = hub.sensors[key]
        self._attr_unique_id = f"{hub.uid}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={hub.hub_identifier})
        unit = info.get("unit")
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = DEVICE_CLASSES.get(unit)
        if info["kind"] == "temperature":
            self._attr_name = info.get("name") or f"Temperature {info['group']}"
            self._attr_extra_state_attributes = {"group_address": info["group"]}
        else:
            self._attr_name = (
                info.get("name")
                or f"Measurement {info['device']}/{info['channel']}"
            )
            self._attr_extra_state_attributes = {
                "device_id": info["device"],
                "channel": info["channel"],
                "unit_code": info["unit_code"],
            }

    async def async_added_to_hass(self) -> None:
        if self._key in self._hub.sensor_values:
            self._attr_native_value = self._hub.sensor_values[self._key]
        elif (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_sensor(self._hub.entry.entry_id, self._key),
                self._update,
            )
        )

    @callback
    def _update(self) -> None:
        self._attr_native_value = self._hub.sensor_values.get(self._key)
        self.async_write_ha_state()
