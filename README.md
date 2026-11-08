# C-Bus Lighting for Home Assistant

Native Home Assistant integration for Clipsal C-Bus lighting. It talks directly
to a C-Bus PC Interface (5500PC, 5500PCU, 5500PACA) over USB serial. No MQTT
bridge or C-Gate needed.

## Features
- **Correct state from the start.** On startup, after any reconnect and every
  30 minutes, it asks the network for the real level of every group (C-Bus
  status requests). No more stale states after a restart.
- **Reliable commands.** Every command carries a confirmation code; if the
  interface doesn't confirm it, it is retried.
- **Dimming done properly.** Brightness and transitions map onto C-Bus ramp
  rates. A ramp to level 0 is reported as off.
- **Only real groups.** Lights are created for groups that exist on output
  units (plus any group seen in network traffic), not 256 placeholders.
- **Wall switches as triggers.** Every lighting command on the network fires a
  `cbus_event` event with `group`, `action`, `level`, `ramp_seconds` and
  `source_unit`.
- **Automatic setup.** A C-Bus USB interface is detected and set up
  automatically.
- **Takes over from cbus2mqtt.** On first run, names, areas and entity IDs of
  existing cbus2mqtt (cmqttd) lights are moved over, and the old MQTT entities
  are disabled. Automations keep working.
- **Group names** can be imported from a Toolkit project (`.cbz`) or C-Gate tag
  file (`.xml`): Settings → Devices & services → C-Bus → Configure.
- Answers network clock requests and (optionally) broadcasts the time every
  5 minutes, like cmqttd did.
- Diagnostics: a *Connection* sensor (with last sync details) and a *Resync*
  button.

## Installing
1. Settings → Marketplace → Add from link → `https://github.com/a1fred0/ha-cbus`
   → Install, then restart Home Assistant.
2. Stop anything else using the interface (the cbus2mqtt app, C-Gate) and turn
   off its *Start on boot*. Only one program can use the serial port.
3. The interface is set up automatically. If not: Settings → Devices &
   services → Add integration → C-Bus Lighting, and pick the serial port.

## Going back to cbus2mqtt
Remove the C-Bus Lighting integration, re-enable the old MQTT light entities
(they were renamed to `light.<name>_cmqttd` and disabled), rename them back,
and start the cbus2mqtt app.

## Notes
- Only the lighting application (56) is supported so far.
- Bridged networks are not supported.

## Development
```
pip install pytest-homeassistant-custom-component pyserial
pytest
```
Tests run against a simulated C-Bus network (`tests/fake_pci.py`).

## Licence
LGPL-3.0-or-later. Protocol details and test vectors were cross-checked
against [micolous/cbus](https://github.com/micolous/cbus) (LGPLv3), which has
been verified on real hardware.
