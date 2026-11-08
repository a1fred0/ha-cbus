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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CMQTTD_BINARY_SENSOR_UNIQUE_ID,
    CMQTTD_LIGHT_UNIQUE_ID,
    CONF_PORT,
    CONF_PROJECT_FILE,
    CONF_RESYNC_MINUTES,
    CONF_TIMESYNC,
    DEFAULT_RESYNC_MINUTES,
    DEFAULT_TIMESYNC,
    DOMAIN,
    EVENT_CBUS,
    LIGHTING_APPLICATION,
    STORAGE_VERSION,
    TIMESYNC_INTERVAL,
    signal_group,
    signal_new_group,
    signal_status,
)
from .project import load_project_file
from .protocol import (
    GROUP_MISSING,
    GROUP_OFF,
    GROUP_ON,
    CBusCommandFailed,
    CBusError,
    CBusNotConnected,
    CBusPCI,
    LightingEvent,
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

        self.pci = CBusPCI(
            self.port,
            on_lighting=self._on_lighting,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            timesync_interval=TIMESYNC_INTERVAL if timesync else 0,
            answer_clock_requests=True,
            port_factory=port_factory,
        )

    # ------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        """Load saved state, migrate old entities and start connecting."""
        self._data = await self._store.async_load() or {}
        for ga, info in self._data.get("groups", {}).items():
            self.groups[int(ga)] = dict(info)
        for ga, level in self._data.get("levels", {}).items():
            self.levels[int(ga)] = level

        if self.project_file:
            try:
                names = await self.hass.async_add_executor_job(
                    load_project_file, self.project_file
                )
                self._project_names = names.get(LIGHTING_APPLICATION, {})
                _LOGGER.info(
                    "Loaded %d group names from %s",
                    len(self._project_names),
                    self.project_file,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Could not read C-Bus project file %s: %s", self.project_file, err
                )
        for ga, name in self._project_names.items():
            info = self.groups.get(ga)
            if info is None or info.get("source") == "default":
                self.groups[ga] = {"name": name, "source": "project"}

        self._register_hub_device()
        if not self._data.get("migrated_cmqttd"):
            self._migrate_from_cmqttd()
            self._data["migrated_cmqttd"] = True
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

    def group_name(self, group: int) -> str:
        info = self.groups.get(group)
        if info and info.get("name"):
            return info["name"]
        return f"C-Bus Group {group}"

    # ------------------------------------------------------------ migration
    def _migrate_from_cmqttd(self) -> None:
        """Take over names, areas and entity IDs from cbus2mqtt (cmqttd)."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        migrated = []
        for ga in range(255):
            old_id = ent_reg.async_get_entity_id(
                "light", "mqtt", CMQTTD_LIGHT_UNIQUE_ID.format(ga)
            )
            if not old_id:
                continue
            old = ent_reg.async_get(old_id)
            if old is None or old.disabled_by is not None:
                continue
            new_uid = self.unique_id_for(ga)
            if ent_reg.async_get_entity_id("light", DOMAIN, new_uid):
                continue
            device = dev_reg.async_get(old.device_id) if old.device_id else None
            name = (
                (device.name_by_user if device else None)
                or old.name
                or (device.name if device else None)
                or old.original_name
                or f"C-Bus Group {ga}"
            )
            area = old.area_id or (device.area_id if device else None)

            # Move the old entity out of the way and disable it.
            ent_reg.async_update_entity(
                old_id,
                new_entity_id=f"{old_id}_cmqttd",
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
            old_bin = ent_reg.async_get_entity_id(
                "binary_sensor", "mqtt", CMQTTD_BINARY_SENSOR_UNIQUE_ID.format(ga)
            )
            if old_bin and ent_reg.async_get(old_bin).disabled_by is None:
                ent_reg.async_update_entity(
                    old_bin, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                )

            new_device = dev_reg.async_get_or_create(
                config_entry_id=self.entry.entry_id,
                identifiers={(DOMAIN, new_uid)},
                name=name,
                manufacturer="Clipsal",
                model="C-Bus lighting group",
                via_device=self.hub_identifier,
            )
            if area:
                dev_reg.async_update_device(new_device.id, area_id=area)
            entry = ent_reg.async_get_or_create(
                "light",
                DOMAIN,
                new_uid,
                config_entry=self.entry,
                device_id=new_device.id,
            )
            if entry.entity_id != old_id:
                ent_reg.async_update_entity(entry.entity_id, new_entity_id=old_id)
            self.groups[ga] = {"name": name, "source": "migrated"}
            migrated.append(f"{ga}={old_id}")
        if migrated:
            _LOGGER.info(
                "Took over %d lights from cbus2mqtt: %s",
                len(migrated),
                ", ".join(migrated),
            )

    # --------------------------------------------------------------- groups
    @callback
    def _add_group(self, group: int) -> None:
        if group in self.groups or group == 255:
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
        if event.action == "terminate":
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

        present = {ga for ga, s in states.items() if s != GROUP_MISSING and ga != 255}
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
        self, group: int, level: int, transition: float | None = None
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
        self._set_level(group, level)
