"""Constants for the C-Bus Lighting integration."""
from __future__ import annotations

DOMAIN = "cbus"

CONF_PORT = "port"
CONF_PROJECT_FILE = "project_file"
CONF_RESYNC_MINUTES = "resync_minutes"
CONF_TIMESYNC = "timesync"

DEFAULT_RESYNC_MINUTES = 30
DEFAULT_TIMESYNC = True
TIMESYNC_INTERVAL = 300

LIGHTING_APPLICATION = 0x38  # 56

EVENT_CBUS = "cbus_event"

STORAGE_VERSION = 1

# cbus2mqtt / cmqttd entities we can take over
CMQTTD_LIGHT_UNIQUE_ID = "cbus_light_{}"
CMQTTD_BINARY_SENSOR_UNIQUE_ID = "cbus_bin_sensor_{}"


def signal_group(entry_id: str, group: int) -> str:
    """Dispatcher signal for a group's state changing."""
    return f"{DOMAIN}_{entry_id}_group_{group}"


def signal_new_group(entry_id: str) -> str:
    """Dispatcher signal for a newly discovered group."""
    return f"{DOMAIN}_{entry_id}_new_group"


def signal_status(entry_id: str) -> str:
    """Dispatcher signal for connection / sync status changes."""
    return f"{DOMAIN}_{entry_id}_status"
