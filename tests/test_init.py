"""Home Assistant integration tests using a simulated C-Bus network."""
from __future__ import annotations

import asyncio

from homeassistant.config_entries import SOURCE_USB, SOURCE_USER
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.service_info.usb import UsbServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cbus.const import DOMAIN

PORT = "/dev/serial/by-id/usb-Silicon_Labs_C-Bus_Pascal_Automation_Controller-if00"


async def _wait(hass: HomeAssistant, cond, timeout: float = 10.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="001004113837",
        data={"port": PORT},
        options={"timesync": False, **options},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _light_ids(hass: HomeAssistant) -> dict[int, str]:
    ent_reg = er.async_get(hass)
    out = {}
    for ga in range(256):
        eid = ent_reg.async_get_entity_id("light", DOMAIN, f"001004113837_56_{ga}")
        if eid:
            out[ga] = eid
    return out


async def test_setup_discovers_groups_and_levels(hass: HomeAssistant, network):
    entry = await _setup(hass)
    hub = entry.runtime_data
    await _wait(hass, lambda: hub.sync_count >= 1)
    ids = _light_ids(hass)
    assert sorted(ids) == [0, 1, 3, 5]
    assert hass.states.get(ids[0]).state == STATE_OFF
    s1 = hass.states.get(ids[1])
    assert s1.state == STATE_ON and s1.attributes["brightness"] == 255
    s3 = hass.states.get(ids[3])
    assert s3.state == STATE_ON and s3.attributes["brightness"] == 128
    conn = hass.states.get("binary_sensor.c_bus_network_connection")
    assert conn.state == STATE_ON
    assert conn.attributes["level_requests_supported"] is True


async def test_commands(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    ids = _light_ids(hass)

    await hass.services.async_call("light", "turn_on", {"entity_id": ids[0]}, blocking=True)
    assert network.groups[0] == 255
    assert hass.states.get(ids[0]).state == STATE_ON

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ids[5], "brightness": 100, "transition": 4},
        blocking=True,
    )
    assert network.groups[5] == 100
    assert network.commands[-1][3:] == bytes([0x0A, 5, 100])

    await hass.services.async_call("light", "turn_off", {"entity_id": ids[1]}, blocking=True)
    assert network.groups[1] == 0
    assert hass.states.get(ids[1]).state == STATE_OFF


async def test_network_events_update_state(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    ids = _light_ids(hass)
    events = []
    hass.bus.async_listen("cbus_event", events.append)

    network.port.emit_lighting(0x0B, bytes([0x79, 0x00]))
    await _wait(hass, lambda: hass.states.get(ids[0]).state == STATE_ON)
    # ramp to zero must be OFF (cmqttd reported this as ON)
    network.port.emit_lighting(0x0D, bytes([0x0A, 0x00, 0x00]))
    await _wait(hass, lambda: hass.states.get(ids[0]).state == STATE_OFF)
    assert [(e.data["group"], e.data["action"], e.data["source_unit"]) for e in events] == [
        (0, "on", 0x0B),
        (0, "ramp", 0x0D),
    ]

    # traffic for an unknown group creates a new light
    network.port.emit_lighting(0x0B, bytes([0x79, 54]))
    await _wait(hass, lambda: 54 in _light_ids(hass))
    await _wait(hass, lambda: hass.states.get(_light_ids(hass)[54]).state == STATE_ON)


async def test_resync_corrects_drift(hass: HomeAssistant, network):
    entry = await _setup(hass)
    hub = entry.runtime_data
    await _wait(hass, lambda: hub.sync_count >= 1)
    ids = _light_ids(hass)
    # Change happens on the network without us seeing it
    network.groups[0] = 200
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.c_bus_network_resync"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(ids[0]).attributes["brightness"] == 200


async def test_disconnect_and_reconnect(hass: HomeAssistant, network):
    entry = await _setup(hass)
    hub = entry.runtime_data
    await _wait(hass, lambda: hub.sync_count >= 1)
    ids = _light_ids(hass)
    network.groups[1] = 0  # changes while we are offline
    network.port.disconnect()
    await _wait(hass, lambda: hass.states.get(ids[1]).state == STATE_UNAVAILABLE)
    await _wait(hass, lambda: hub.sync_count >= 2, timeout=15)
    assert hass.states.get(ids[1]).state == STATE_OFF


async def test_levels_fall_back_to_binary(hass: HomeAssistant, network):
    network.level_requests_supported = False
    entry = await _setup(hass)
    hub = entry.runtime_data
    await _wait(hass, lambda: hub.sync_count >= 1, timeout=20)
    ids = _light_ids(hass)
    assert hass.states.get(ids[3]).attributes["brightness"] == 255
    assert hass.states.get(ids[0]).state == STATE_OFF
    assert hub.pci.level_requests_supported is False


async def test_takes_over_cbus2mqtt_entities(hass: HomeAssistant, network):
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area = ar.async_get(hass).async_create("Living Room")
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    dev = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        identifiers={("mqtt", "cbus_light_1")},
        name="C-Bus Light 001",
    )
    dev_reg.async_update_device(dev.id, name_by_user="Dining", area_id=area.id)
    old = ent_reg.async_get_or_create(
        "light", "mqtt", "cbus_light_1", device_id=dev.id,
        config_entry=mqtt_entry, suggested_object_id="dining",
    )
    ent_reg.async_get_or_create(
        "binary_sensor", "mqtt", "cbus_bin_sensor_1", device_id=dev.id,
        config_entry=mqtt_entry,
    )
    assert old.entity_id == "light.dining"

    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)

    new = ent_reg.async_get("light.dining")
    assert new.platform == DOMAIN
    old_now = ent_reg.async_get("light.dining_cmqttd")
    assert old_now.platform == "mqtt" and old_now.disabled_by is not None
    bin_now = ent_reg.async_get_entity_id("binary_sensor", "mqtt", "cbus_bin_sensor_1")
    assert ent_reg.async_get(bin_now).disabled_by is not None
    new_dev = dev_reg.async_get(new.device_id)
    assert new_dev.name == "Dining" and new_dev.area_id == area.id
    assert hass.states.get("light.dining").state == STATE_ON


async def test_project_file_names(hass: HomeAssistant, network, tmp_path):
    xml = tmp_path / "proj.xml"
    xml.write_text(
        "<Installation><Project><Network><Application><TagName>Lighting</TagName>"
        "<Address>56</Address><Group><TagName>Living Room</TagName><Address>0</Address>"
        "</Group><Group><TagName>&lt;Unused&gt;</TagName><Address>255</Address></Group>"
        "</Application></Network></Project></Installation>"
    )
    entry = await _setup(hass, project_file=str(xml))
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    ids = _light_ids(hass)
    assert hass.states.get(ids[0]).name == "Living Room"
    assert hass.states.get(ids[1]).name == "C-Bus Group 1"


async def test_usb_discovery_creates_entry(hass: HomeAssistant, network):
    info = UsbServiceInfo(
        device="/dev/ttyUSB0", vid="10C4", pid="EA60", serial_number="001004113837",
        manufacturer="Silicon Labs", description="C-Bus Pascal Automation Controller (5500PACA)",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USB}, data=info
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "001004113837"
    await hass.async_block_till_done()
    # second discovery aborts
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USB}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.config_entries.async_unload(
        hass.config_entries.async_entries(DOMAIN)[0].entry_id
    )


async def test_user_flow_shows_form(hass: HomeAssistant, network):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM


async def test_unload(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert network.port.closed
