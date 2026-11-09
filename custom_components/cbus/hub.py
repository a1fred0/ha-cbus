"""Connection to the C-Bus network, shared by all entities."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GROUP_TYPES,
    CONF_PORT,
    COVER_COMMAND_LEVELS,
    CONF_PROJECT_FILE,
    CONF_RESYNC_MINUTES,
    CONF_TIMESYNC,
    DEFAULT_RESYNC_MINUTES,
    DEFAULT_TIMESYNC,
    DOMAIN,
    EVENT_CBUS,
    GROUP_TYPE_DOMAIN,
    GROUP_TYPES,
    LIGHTING_APPLICATION,
    MEASUREMENT_UNITS,
    STORAGE_VERSION,
    TEMPERATURE_APPLICATION,
    TIMESYNC_INTERVAL,
    TRIGGER_APPLICATION,
    TYPE_COVER,
    TYPE_LIGHT,
    signal_group,
    signal_new_group,
    signal_new_sensor,
    signal_new_trigger,
    signal_new_unit,
    signal_sensor,
    signal_status,
    signal_trigger,
    signal_unit,
)
from .project import Project, load_project
from .protocol import (
    GROUP_MISSING,
    GROUP_OFF,
    GROUP_ON,
    CBusCommandFailed,
    CBusError,
    CBusNotConnected,
    CBusPCI,
    LightingEvent,
    MeasurementReading,
    TemperatureReading,
    TriggerEvent,
    is_network_port,
)

_LOGGER = logging.getLogger(__name__)


class CBusHub:
    """Owns the PCI connection and the state of every lighting group."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        port_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.port: str = entry.data[CONF_PORT]
        self.uid: str = entry.unique_id or entry.entry_id
        options = entry.options
        self.resync_minutes: int = options.get(
            CONF_RESYNC_MINUTES, DEFAULT_RESYNC_MINUTES
        )
        self.project_file: str | None = options.get(CONF_PROJECT_FILE) or None
        timesync = options.get(CONF_TIMESYNC, DEFAULT_TIMESYNC)

        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._data: dict[str, Any] = {}
        # group address -> {"name": str, "source": "migrated"|"project"|"default"}
        self.groups: dict[int, dict[str, str]] = {}
        self.levels: dict[int, int | None] = {}
        self.last_sync: datetime | None = None
        self.last_sync_ok: bool | None = None
        self.sync_count = 0
        self._sync_lock = asyncio.Lock()
        self._refresh_handles: dict[int, Callable[[], None]] = {}
        self._project_names: dict[int, str] = {}
        self._project = Project()
        # group addresses the user removed: never re-added automatically
        self.ignored: set[int] = set()
        # what each group controls (light, switch, cover, fan); default light
        self.group_types: dict[int, str] = {
            int(ga): kind
            for ga, kind in (options.get(CONF_GROUP_TYPES) or {}).items()
            if kind in GROUP_TYPES
        }
        # units (wall switches etc) seen sending commands: address -> info
        self.units: dict[int, dict[str, Any]] = {}
        # trigger (scene) groups seen: group -> info
        self.triggers: dict[int, dict[str, Any]] = {}
        # sensors seen: key -> info (kind, name, unit, ...)
        self.sensors: dict[str, dict[str, Any]] = {}
        self.sensor_values: dict[str, float] = {}

        self.pci = CBusPCI(
            self.port,
            on_lighting=self._on_lighting,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_status=self._on_status,
            on_packet=self._on_packet,
            timesync_interval=TIMESYNC_INTERVAL if timesync else 0,
            answer_clock_requests=True,
            port_factory=port_factory,
        )

    # ------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        """Load saved state and start connecting."""
        self._data = await self._store.async_load() or {}
        for ga, info in self._data.get("groups", {}).items():
            self.groups[int(ga)] = dict(info)
        for ga, level in self._data.get("levels", {}).items():
            self.levels[int(ga)] = level
        self.ignored = {int(ga) for ga in self._data.get("ignored", [])}
        for addr, info in self._data.get("units", {}).items():
            self.units[int(addr)] = dict(info)
        for ga, info in self._data.get("triggers", {}).items():
            self.triggers[int(ga)] = dict(info)
        self.sensors = {k: dict(v) for k, v in self._data.get("sensors", {}).items()}

        if self.project_file:
            try:
                self._project = await self.hass.async_add_executor_job(
                    load_project, self.project_file
                )
                self._project_names = self._project.groups.get(LIGHTING_APPLICATION, {})
                _LOGGER.info(
                    "Loaded %d group and %d unit names from %s",
                    len(self._project_names),
                    len(self._project.units),
                    self.project_file,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Could not read C-Bus project file %s: %s", self.project_file, err
                )
        for ga, name in self._project_names.items():
            if ga in self.ignored:
                continue
            info = self.groups.get(ga)
            if info is None or info.get("source") == "default":
                self.groups[ga] = {"name": name, "source": "project"}
        for addr, unit in self._project.units.items():
            info = self.units.get(addr)
            if info is not None:
                info.update(name=unit.name, model=unit.catalog or unit.unit_type)
            elif unit.is_input:
                self.units[addr] = {
                    "name": unit.name,
                    "model": unit.catalog or unit.unit_type,
                }
        for ga, name in self._project.groups.get(TRIGGER_APPLICATION, {}).items():
            self.triggers.setdefault(ga, {})["name"] = name

        self._register_hub_device()
        self._remove_stale_group_entities()
        self._save()

        await self.pci.start()
        if self.resync_minutes:
            self.entry.async_on_unload(
                async_track_time_interval(
                    self.hass,
                    self._periodic_resync,
                    timedelta(minutes=self.resync_minutes),
                    name="C-Bus periodic resync",
                )
            )

    async def async_stop(self) -> None:
        for cancel in self._refresh_handles.values():
            cancel()
        self._refresh_handles.clear()
        await self.pci.stop()
        await self._store.async_save(self._snapshot())

    # ---------------------------------------------------------- persistence
    def _snapshot(self) -> dict[str, Any]:
        data = dict(self._data)
        data["groups"] = {str(ga): info for ga, info in sorted(self.groups.items())}
        data["levels"] = {
            str(ga): lvl for ga, lvl in sorted(self.levels.items()) if lvl is not None
        }
        data["ignored"] = sorted(self.ignored)
        data["units"] = {str(a): info for a, info in sorted(self.units.items())}
        data["triggers"] = {str(g): info for g, info in sorted(self.triggers.items())}
        data["sensors"] = dict(sorted(self.sensors.items()))
        return data

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._snapshot, 5)

    # -------------------------------------------------------------- devices
    def unique_id_for(self, group: int) -> str:
        return f"{self.uid}_{LIGHTING_APPLICATION}_{group}"

    @property
    def hub_identifier(self) -> tuple[str, str]:
        return (DOMAIN, self.uid)

    def _register_hub_device(self) -> None:
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={self.hub_identifier},
            name="C-Bus Network",
            manufacturer="Clipsal",
            model="C-Bus PC Interface",
        )

    @property
    def is_network(self) -> bool:
        """Whether the interface is reached over the network (CNI)."""
        return is_network_port(self.port)

    def group_type(self, group: int) -> str:
        """What a group controls: light (default), light_onoff, switch, ..."""
        return self.group_types.get(group, TYPE_LIGHT)

    def _remove_stale_group_entities(self) -> None:
        """Remove a group's entity from platforms it no longer belongs to.

        When a group's type changes (say light -> cover) the old entity has the
        same unique ID on another platform.
        """
        ent_reg = er.async_get(self.hass)
        domains = set(GROUP_TYPE_DOMAIN.values())
        for ga in set(self.groups) | set(self.group_types):
            want = GROUP_TYPE_DOMAIN[self.group_type(ga)]
            uid = self.unique_id_for(ga)
            for domain in domains - {want}:
                if entity_id := ent_reg.async_get_entity_id(domain, DOMAIN, uid):
                    _LOGGER.info(
                        "C-Bus group %d is now a %s; removing %s", ga, want, entity_id
                    )
                    ent_reg.async_remove(entity_id)

    def unit_name(self, unit: int) -> str:
        info = self.units.get(unit)
        if info and info.get("name"):
            return info["name"]
        return f"C-Bus unit {unit}"

    def trigger_name(self, group: int) -> str:
        info = self.triggers.get(group)
        if info and info.get("name"):
            return info["name"]
        return f"Trigger group {group}"

    def group_name(self, group: int) -> str:
        info = self.groups.get(group)
        if info and info.get("name"):
            return info["name"]
        return f"C-Bus Group {group}"

    # --------------------------------------------------------------- groups
    @callback
    def _add_group(self, group: int) -> None:
        if group in self.groups or group == 255 or group in self.ignored:
            return
        name = self._project_names.get(group)
        self.groups[group] = {
            "name": name or f"C-Bus Group {group}",
            "source": "project" if name else "default",
        }
        _LOGGER.info("Discovered C-Bus group %d (%s)", group, self.group_name(group))
        self._save()
        async_dispatcher_send(self.hass, signal_new_group(self.entry.entry_id), group)

    @callback
    def async_remove_group(self, group: int) -> None:
        """Forget a group and never re-add it automatically."""
        uid = self.unique_id_for(group)
        ent_reg = er.async_get(self.hass)
        for domain in set(GROUP_TYPE_DOMAIN.values()):
            if entity_id := ent_reg.async_get_entity_id(domain, DOMAIN, uid):
                ent_reg.async_remove(entity_id)
        dev_reg = dr.async_get(self.hass)
        if device := dev_reg.async_get_device(identifiers={(DOMAIN, uid)}):
            dev_reg.async_remove_device(device.id)
        self.groups.pop(group, None)
        self.levels.pop(group, None)
        self.ignored.add(group)
        self._save()
        _LOGGER.info("Removed C-Bus group %d", group)

    @callback
    def async_add_group(self, group: int) -> None:
        """Add a group by hand (also undoes a removal)."""
        self.ignored.discard(group)
        self._add_group(group)
        self._save()

    @callback
    def _set_level(self, group: int, level: int | None) -> None:
        if self.levels.get(group, -1) == level:
            return
        self.levels[group] = level
        self._save()
        async_dispatcher_send(self.hass, signal_group(self.entry.entry_id, group))

    # ------------------------------------------------------------ callbacks
    @callback
    def _on_connected(self) -> None:
        _LOGGER.info("Connected to C-Bus on %s", self.port)
        async_dispatcher_send(self.hass, signal_status(self.entry.entry_id))
        self.entry.async_create_background_task(
            self.hass, self.async_resync(), "cbus-resync"
        )

    @callback
    def _on_status(self) -> None:
        issue_id = f"pac_programming_mode_{self.entry.entry_id}"
        if self.pci.pac_programming_mode:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="pac_programming_mode",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        async_dispatcher_send(self.hass, signal_status(self.entry.entry_id))

    @callback
    def _on_disconnected(self) -> None:
        _LOGGER.warning("Disconnected from C-Bus on %s", self.port)
        async_dispatcher_send(self.hass, signal_status(self.entry.entry_id))

    @callback
    def _on_lighting(self, event: LightingEvent) -> None:
        if event.application != LIGHTING_APPLICATION or event.group == 255:
            return
        group = event.group
        if group not in self.groups:
            self._add_group(group)
        if group not in self.groups:
            pass  # removed by the user: only report the event
        elif (
            self.group_type(group) == TYPE_COVER
            and event.level in COVER_COMMAND_LEVELS
        ):
            pass  # a shutter command (stop, toggle), not a position
        elif event.action == "terminate":
            self._schedule_refresh(group, 1.0)
        elif event.level is not None:
            self._set_level(group, event.level)
        self.hass.bus.async_fire(
            EVENT_CBUS,
            {
                "group": group,
                "name": self.group_name(group),
                "action": event.action,
                "level": event.level,
                "ramp_seconds": event.ramp_seconds,
                "source_unit": event.source,
                "application": event.application,
            },
        )

    @callback
    def _on_packet(self, messages: list) -> None:
        """Everything decoded from one packet on the network."""
        lighting = [
            m
            for m in messages
            if isinstance(m, LightingEvent)
            and m.application == LIGHTING_APPLICATION
            and m.source is not None
            and m.group != 255
        ]
        if lighting:
            self._unit_event(lighting)
        for msg in messages:
            if isinstance(msg, TriggerEvent):
                self._on_trigger(msg)
            elif isinstance(msg, TemperatureReading):
                self._on_sensor(
                    f"temperature_{msg.group}",
                    {
                        "kind": "temperature",
                        "group": msg.group,
                        "unit": "°C",
                    },
                    msg.celsius,
                )
            elif isinstance(msg, MeasurementReading):
                self._on_sensor(
                    f"measurement_{msg.device}_{msg.channel}",
                    {
                        "kind": "measurement",
                        "device": msg.device,
                        "channel": msg.channel,
                        "unit_code": msg.unit_code,
                        "unit": MEASUREMENT_UNITS.get(msg.unit_code),
                    },
                    msg.value,
                )

    @callback
    def _unit_event(self, events: list[LightingEvent]) -> None:
        """Turn the lighting commands from one unit into a switch event.

        Wall switches only send lighting commands, so a press is recognised
        from them: one command is "on"/"off"/"ramp"/"terminate"; several in
        one packet are "all_off", "all_on" or "scene".
        """
        unit = events[0].source
        if unit not in self.units:
            self.units[unit] = {}
            self._save()
            _LOGGER.info("Discovered C-Bus unit %d", unit)
            async_dispatcher_send(self.hass, signal_new_unit(self.entry.entry_id), unit)

        def off(e: LightingEvent) -> bool:
            return e.action == "off" or (e.action == "ramp" and e.level == 0)

        def on(e: LightingEvent) -> bool:
            return e.action == "on" or (e.action == "ramp" and e.level == 255)

        if len(events) == 1:
            event_type = events[0].action
        elif all(off(e) for e in events):
            event_type = "all_off"
        elif all(on(e) for e in events):
            event_type = "all_on"
        else:
            event_type = "scene"
        data = {
            "groups": [e.group for e in events],
            "group_names": [self.group_name(e.group) for e in events],
            "levels": [e.level for e in events],
            "ramp_seconds": max(e.ramp_seconds for e in events),
        }
        if len(events) == 1:
            data["group"] = events[0].group
            data["level"] = events[0].level
        async_dispatcher_send(
            self.hass, signal_unit(self.entry.entry_id, unit), event_type, data
        )

    @callback
    def _on_trigger(self, msg: TriggerEvent) -> None:
        if msg.group not in self.triggers:
            self.triggers[msg.group] = {}
            self._save()
            async_dispatcher_send(
                self.hass, signal_new_trigger(self.entry.entry_id), msg.group
            )
        data = {"action_selector": msg.action_selector, "source_unit": msg.source}
        async_dispatcher_send(
            self.hass,
            signal_trigger(self.entry.entry_id, msg.group),
            msg.action,
            data,
        )
        self.hass.bus.async_fire(
            EVENT_CBUS,
            {
                "group": msg.group,
                "name": self.trigger_name(msg.group),
                "action": msg.action,
                "action_selector": msg.action_selector,
                "source_unit": msg.source,
                "application": TRIGGER_APPLICATION,
            },
        )

    @callback
    def _on_sensor(self, key: str, info: dict[str, Any], value: float) -> None:
        if key not in self.sensors:
            if info["kind"] == "temperature":
                names = self._project.groups.get(TEMPERATURE_APPLICATION, {})
                info["name"] = names.get(info["group"])
            self.sensors[key] = info
            self._save()
            async_dispatcher_send(self.hass, signal_new_sensor(self.entry.entry_id), key)
        self.sensor_values[key] = value
        async_dispatcher_send(self.hass, signal_sensor(self.entry.entry_id, key))

    async def async_trigger(self, group: int, action_selector: int) -> None:
        """Send a Trigger Control (scene) command."""
        try:
            await self.pci.trigger(group, action_selector)
        except CBusNotConnected as err:
            raise HomeAssistantError("C-Bus interface is not connected") from err
        except CBusCommandFailed as err:
            raise HomeAssistantError(
                f"C-Bus did not confirm trigger group {group}"
            ) from err

    # ----------------------------------------------------------------- sync
    @callback
    def _schedule_refresh(self, group: int, delay: float) -> None:
        block = group & 0xE0
        if block in self._refresh_handles:
            self._refresh_handles.pop(block)()

        @callback
        def _run(_now: datetime) -> None:
            self._refresh_handles.pop(block, None)
            self.entry.async_create_background_task(
                self.hass, self.async_refresh_block(block), "cbus-refresh"
            )

        self._refresh_handles[block] = async_call_later(self.hass, delay, _run)

    async def async_refresh_block(self, block: int) -> None:
        """Re-read the levels of one block of 32 groups."""
        if not self.pci.connected or self.pci.level_requests_supported is False:
            return
        async with self._sync_lock:
            try:
                levels = await self.pci.level_status(block)
            except CBusError as err:
                _LOGGER.debug("Level refresh failed: %s", err)
                return
        for group, level in levels.items():
            if group in self.groups and level is not None:
                self._set_level(group, level)

    async def _periodic_resync(self, _now: datetime) -> None:
        await self.async_resync()

    async def async_resync(self) -> bool:
        """Read the real state of every group from the network."""
        if not self.pci.connected:
            return False
        async with self._sync_lock:
            ok = await self._resync()
        self.last_sync = dt_util.utcnow()
        self.last_sync_ok = ok
        self.sync_count += 1
        async_dispatcher_send(self.hass, signal_status(self.entry.entry_id))
        return ok

    async def _resync(self) -> bool:
        try:
            states = await self.pci.binary_status()
        except CBusError as err:
            _LOGGER.warning("C-Bus status request failed: %s", err)
            return False
        if len(states) < 256:
            _LOGGER.warning(
                "Incomplete C-Bus status reply (%d of 256 groups)", len(states)
            )
            if not states:
                return False

        present = {
            ga
            for ga, s in states.items()
            if s != GROUP_MISSING and ga != 255 and ga not in self.ignored
        }
        for ga in sorted(present):
            self._add_group(ga)
        targets = present | set(self.groups)

        levels: dict[int, int | None] = {}
        if self.pci.level_requests_supported is not False:
            for block in sorted({ga & 0xE0 for ga in targets}):
                try:
                    reply = await self.pci.level_status(block)
                except CBusError as err:
                    _LOGGER.debug("Level request failed: %s", err)
                    reply = {}
                if reply:
                    self.pci.level_requests_supported = True
                    levels.update(reply)
                elif self.pci.level_requests_supported is None:
                    _LOGGER.warning(
                        "C-Bus interface did not answer level requests; using "
                        "on/off status only"
                    )
                    self.pci.level_requests_supported = False
                    break

        for ga in sorted(targets):
            level = levels.get(ga)
            if level is None:
                state = states.get(ga)
                if state == GROUP_ON:
                    current = self.levels.get(ga)
                    level = current if current else 255
                elif state == GROUP_OFF:
                    level = 0
                else:
                    # Not on any output unit (or no reply): keep what we know
                    # from network traffic.
                    continue
            self._set_level(ga, level)
        _LOGGER.debug(
            "Resync complete: %s",
            {ga: self.levels.get(ga) for ga in sorted(targets)},
        )
        return True

    # ------------------------------------------------------------- commands
    async def async_set_level(
        self,
        group: int,
        level: int,
        transition: float | None = None,
        *,
        update_state: bool = True,
    ) -> None:
        """Set a group to a level (0-255), optionally over a transition."""
        transition = transition or 0
        level = max(0, min(255, int(level)))
        try:
            if transition < 0.5 and level == 255:
                await self.pci.lighting_on(group)
            elif transition < 0.5 and level == 0:
                await self.pci.lighting_off(group)
            else:
                await self.pci.lighting_ramp(group, level, transition)
        except CBusNotConnected as err:
            raise HomeAssistantError("C-Bus interface is not connected") from err
        except CBusCommandFailed as err:
            raise HomeAssistantError(
                f"C-Bus did not confirm the command for group {group}"
            ) from err
        if update_state:
            self._set_level(group, level)
