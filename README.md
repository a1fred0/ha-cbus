# C-Bus Lighting for Home Assistant

Native Home Assistant integration for Clipsal C-Bus lighting, talking directly to a
C-Bus PCI / PAC over USB serial. No MQTT bridge or C-Gate required.

**Status: work in progress.** The current version is a placeholder.

## Planned features
- Direct serial connection to a C-Bus PC Interface (5500PC / 5500PACA)
- Startup and reconnect level sync (no stale states after restarts)
- Lights with dimming and transitions mapped to C-Bus ramp rates
- Correct handling of ramp-to-zero (reported as off)
- Acknowledged command queue with retries
- Group names imported from a C-Gate / Toolkit project file
- Wall switch presses as Home Assistant events

Built on the C-Bus protocol work in [micolous/cbus](https://github.com/micolous/cbus) (LGPLv3).
