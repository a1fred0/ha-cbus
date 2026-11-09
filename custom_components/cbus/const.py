"""Constants for the C-Bus Lighting integration."""
from __future__ import annotations

DOMAIN = "cbus"

CONF_PORT = "port"
CONF_PROJECT_FILE = "project_file"
CONF_RESYNC_MINUTES = "resync_minutes"
CONF_TIMESYNC = "timesync"
CONF_GROUP_TYPES = "group_types"
CONF_PROJECT_UPLOAD = "project_upload"

DEFAULT_RESYNC_MINUTES = 30
DEFAULT_TIMESYNC = True
TIMESYNC_INTERVAL = 300

LIGHTING_APPLICATION = 0x38  # 56
TEMPERATURE_APPLICATION = 0x19  # 25
TRIGGER_APPLICATION = 0xCA  # 202

# What a lighting group address controls, and the entity platform for each.
TYPE_LIGHT = "light"  # dimmable light (default)
TYPE_LIGHT_ONOFF = "light_onoff"  # relay: on/off light
TYPE_SWITCH = "switch"
TYPE_COVER = "cover"  # shutter relay: 0 closed, 255 open
TYPE_FAN = "fan"  # sweep fan controller: off / low / medium / high
GROUP_TYPES = (TYPE_LIGHT, TYPE_LIGHT_ONOFF, TYPE_SWITCH, TYPE_COVER, TYPE_FAN)
GROUP_TYPE_DOMAIN = {
    TYPE_LIGHT: "light",
    TYPE_LIGHT_ONOFF: "light",
    TYPE_SWITCH: "switch",
    TYPE_COVER: "cover",
    TYPE_FAN: "fan",
}

# Shutter relay levels
COVER_OPEN = 255
COVER_CLOSED = 0
COVER_STOP = 5  # 2%: "stop" in shutter relay level translation
# Levels a shutter relay treats as commands, not positions: 1% (close/stop),
# 2% (stop; other devices may send 4-6), 98% (toggle), 99% (open/stop).
COVER_COMMAND_LEVELS = frozenset({3, 4, 5, 6, 250, 252})

# Fan controller levels for off / low / medium / high
FAN_LEVELS = (0, 86, 170, 255)

# Measurement application unit codes we know (others are shown as codes).
# (Schneider Commission help, "Measurement application"; codes 0x1A-0x27
# exist but aren't documented publicly.)
MEASUREMENT_UNITS = {
    0x00: "°C",
    0x01: "A",
    0x02: "°",
    0x03: "C",
    0x04: None,  # boolean
    0x05: "F",
    0x06: "H",
    0x07: "Hz",
    0x08: "J",
    0x09: "kat",
    0x0A: "kg/m³",
    0x0B: "kg",
    0x0C: "L",
    0x0D: "L/h",
    0x0E: "L/min",
    0x0F: "L/s",
    0x10: "lx",
    0x11: "m",
    0x12: "m/min",
    0x13: "m/s",
    0x14: "m/s²",
    0x15: "mol",
    0x16: "N·m",
    0x17: "N",
    0x18: "Ω",
    0x19: "Pa",
}

# Event types for a switch / key unit (built from its lighting commands).
UNIT_EVENT_TYPES = ["on", "off", "ramp", "terminate", "all_on", "all_off", "scene"]
TRIGGER_EVENT_TYPES = ["trigger", "indicator_kill"]

EVENT_CBUS = "cbus_event"

STORAGE_VERSION = 1


def signal_group(entry_id: str, group: int) -> str:
    """Dispatcher signal for a group's state changing."""
    return f"{DOMAIN}_{entry_id}_group_{group}"


def signal_new_group(entry_id: str) -> str:
    """Dispatcher signal for a newly discovered group."""
    return f"{DOMAIN}_{entry_id}_new_group"


def signal_status(entry_id: str) -> str:
    """Dispatcher signal for connection / sync status changes."""
    return f"{DOMAIN}_{entry_id}_status"


def signal_new_unit(entry_id: str) -> str:
    """Dispatcher signal for a newly seen unit (wall switch etc)."""
    return f"{DOMAIN}_{entry_id}_new_unit"


def signal_unit(entry_id: str, unit: int) -> str:
    """Dispatcher signal for a unit's event."""
    return f"{DOMAIN}_{entry_id}_unit_{unit}"


def signal_new_trigger(entry_id: str) -> str:
    """Dispatcher signal for a newly seen trigger group."""
    return f"{DOMAIN}_{entry_id}_new_trigger"


def signal_trigger(entry_id: str, group: int) -> str:
    """Dispatcher signal for a trigger group's event."""
    return f"{DOMAIN}_{entry_id}_trigger_{group}"


def signal_new_sensor(entry_id: str) -> str:
    """Dispatcher signal for a newly seen sensor."""
    return f"{DOMAIN}_{entry_id}_new_sensor"


def signal_sensor(entry_id: str, key: str) -> str:
    """Dispatcher signal for a sensor reading."""
    return f"{DOMAIN}_{entry_id}_sensor_{key}"
