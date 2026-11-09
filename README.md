# C-Bus for Home Assistant

Native Home Assistant integration for Clipsal C-Bus. It talks directly to a
C-Bus PC Interface (5500PC, 5500PCU, 5500PACA) over USB serial, or to an
Ethernet interface (5500CN). No MQTT bridge or C-Gate needed.

**Maintenance:** this is maintained in spare time, on a best-effort basis.
Bug reports with a debug log are welcome, and pull requests even more so.
Features marked untested below depend on reports from people who have that
hardware.

## What's tested and what isn't
This was written for, and tested on, one real installation: a 5500PACA on USB,
relay and dimmer channels, and Neo key units. Everything marked
**untested** below follows the C-Bus documentation and other projects'
experience, but hasn't been tried on real hardware. If you have that hardware,
please [open an issue](https://github.com/a1fred0/ha-cbus/issues) and say
whether it works, with a debug log if it doesn't.

| Feature | Status |
|---|---|
| Lights: dimmable, on/off (relay) | Tested |
| State read from the network at startup / reconnect / every 30 min | Tested |
| Acknowledged commands with retries | Tested |
| Wall switch presses as events (incl. "all off" long presses) | Tested |
| Group / unit names from a Toolkit `.cbz` or C-Gate `.xml` project | Tested (C-Gate XML) |
| Switches (group as a plain on/off switch) | Untested (simple) |
| Blinds / curtains on shutter relays → covers | **Untested** |
| Fan controllers → fans | **Untested** |
| Scenes (Trigger Control): events and `cbus.trigger` | **Untested** |
| Temperature Broadcast and Measurement → sensors | **Untested** |
| Ethernet interface (5500CN) over TCP | **Untested** |
| Security, HVAC, Enable Control, Media Transport, bridged networks | Not supported yet |

## Features
- **Correct state from the start.** On startup, after any reconnect and every
  30 minutes, it asks the network for the real level of every group (C-Bus
  status requests). No more stale states after a restart.
- **Reliable commands.** Every command carries a confirmation code; if the
  interface doesn't confirm it, it is retried.
- **Dimming done properly.** Brightness and transitions map onto C-Bus ramp
  rates. A ramp to level 0 is reported as off.
- **Only real groups.** Entities are created for groups that exist on output
  units (plus any group seen in network traffic), not 256 placeholders.
  Unwanted ones can be removed with `cbus.remove_group` and stay removed.
- **Wall switches as event entities.** Each switch (key unit) gets an
  *event* entity (e.g. `event.kitchen_presses`). C-Bus switches only send
  ordinary lighting commands, so the event types are built from them:
  - `on`, `off`, `ramp` (dim or set level), `terminate` (dimming stopped):
    one group, in the `group` attribute;
  - `all_off`, `all_on`, `scene`: one press that sent several commands at once
    (e.g. a long press that switches everything off). The groups are in the
    `groups` attribute.

  Key units keep track of the level of their groups (they listen to all
  traffic, including Home Assistant's commands), and their On/Off/Toggle key
  functions only send commands that change something: a long press set to
  "Off" sends OFF only for the groups that are on. So `groups` lists what was
  actually switched.
- **Every command on the network** also fires a `cbus_event` event with
  `group`, `action`, `level`, `ramp_seconds` and `source_unit`.
- **Choose what each group controls** (Settings → Devices & services → C-Bus
  → Configure → What each group controls): dimmable light (default), on/off
  light, switch, blind/curtain (shutter relay), or fan.
- **Names** for groups, switches, scenes and temperature sensors can be
  imported from a Toolkit project (`.cbz`) or C-Gate tag file (`.xml`):
  upload it under Configure → Settings.
- **Automatic setup.** A C-Bus USB interface is detected and set up
  automatically.
- Answers network clock requests and (optionally) broadcasts the time every
  5 minutes, like cmqttd did.
- Diagnostics: a *Connection* sensor (with last sync details) and a *Resync*
  button.

### Blinds / curtains (untested)
Shutter relays are controlled through the lighting application, the same way
a wall plate's shutter function does it: open = level 255, close = 0,
stop = 2% (level 5). With *level translation* enabled on the relay, 0%, 3–97%
and 100% move the blind approximately; 1%, 2%, 98% and 99% are commands
(close/stop, stop, toggle, open/stop), so positions are kept clear of them and
those commands from wall plates don't change the position shown. The relay
doesn't report movement, so the position shown is the last one set.

### Fans (untested)
Fan controller channels: off, low, medium, high = levels 0, 86, 170, 255
(0/33/66/100%). The fan controller's own speed thresholds are configurable;
levels in between are shown as the nearest speed.

### Scenes (untested)
Trigger Control commands seen on the network create an event entity per
trigger group (`trigger` with `action_selector`, or `indicator_kill`). To set
off a scene: `cbus.trigger` with `group` and `action_selector`.

### Sensors (untested)
Temperature Broadcast (application 25) and Measurement (application 228)
readings create sensors the first time they're seen, with units from the
Measurement application's unit table (codes 0x00–0x19); other codes are shown
in the `unit_code` attribute.

## Installing
1. Settings → Marketplace → Add from link → `https://github.com/a1fred0/ha-cbus`
   → Install, then restart Home Assistant.
2. Stop anything else using the interface (the cbus2mqtt app, C-Gate) and turn
   off its *Start on boot*. Only one program can use the serial port.
3. A USB interface is set up automatically. If not, or for an Ethernet
   interface: Settings → Devices & services → Add integration → C-Bus, and pick
   the serial port or type `tcp://<address>:10001`.

## Using a Pascal Automation Controller (5500PACA) as the interface
A PAC can act as a PC Interface over its USB port, with two caveats:
- **Its own logic program does not run** while it is acting as a PC Interface.
- It switches into PC Interface mode only from a fresh start. If it is stuck
  in its programming mode (it replies with binary `FF F0 00 06 FE 0B` frames),
  restart it: power-cycle the C-Bus network, or unplug and replug its USB
  cable. Resetting USB from the computer is not enough. The integration
  detects this, raises a repair notice, stops sending, and reconnects by
  itself afterwards.

For a permanent installation, a dedicated 5500PCU (USB) or 5500CN (Ethernet)
interface is better: it is always in PC Interface mode and the PAC keeps
running its logic.

## Actions
- `cbus.remove_group` / `cbus.add_group`: hide a group address for good, or
  add one by hand.
- `cbus.trigger`: send a Trigger Control (scene) command (untested).
- Diagnostics: `cbus.reconnect`, `cbus.usb_reset`, `cbus.raw_open`,
  `cbus.raw_send`, `cbus.raw_lines`, `cbus.raw_baud`. Enable debug logging for
  `custom_components.cbus` to see the raw traffic.

## Moving from cbus2mqtt
The integration does not touch the old MQTT entities. After stopping the
cbus2mqtt app, disable or delete its lights yourself (Settings → Entities). To
keep automations working, give the new C-Bus lights the old entity IDs.

## Notes
- Only lighting application 56 is used for lights, switches, blinds and fans.
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
