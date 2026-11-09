"""Tests for switch events, scenes, sensors, group types and transports."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.cbus.const import DOMAIN
from custom_components.cbus.protocol import (
    MeasurementReading,
    TemperatureReading,
    TriggerEvent,
    _SerialPort,
    decode_line,
    to_serial_url,
    with_checksum,
)
from base64 import b16encode

from tests.test_init import _light_ids, _setup, _wait


def _line(raw: bytes) -> bytes:
    return b16encode(with_checksum(raw))


def _entity(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


# ------------------------------------------------------------- decoding
def test_decode_trigger():
    msgs = decode_line(_line(bytes([0x05, 0x0B, 0xCA, 0x00, 0x02, 0x03, 0x07, 0x79, 0x04, 0x09, 0x05])))
    assert msgs == [
        TriggerEvent(11, 3, 7),
        TriggerEvent(11, 4, 255),
        TriggerEvent(11, 5, None, "indicator_kill"),
    ]


def test_decode_temperature():
    msgs = decode_line(_line(bytes([0x05, 0x20, 0x19, 0x00, 0x02, 0x04, 0x5A])))
    assert msgs == [TemperatureReading(0x20, 4, 22.5)]


def test_decode_measurement():
    # device 1, channel 2, °C, exponent -1, value 215 -> 21.5
    msgs = decode_line(
        _line(bytes([0x05, 0x21, 0xE4, 0x00, 0x0E, 0x01, 0x02, 0x00, 0xFF, 0x00, 0xD7]))
    )
    assert msgs == [MeasurementReading(0x21, 1, 2, 0, 21.5)]
    # negative value
    msgs = decode_line(
        _line(bytes([0x05, 0x21, 0xE4, 0x00, 0x0E, 0x01, 0x02, 0x00, 0x00, 0xFF, 0xF6]))
    )
    assert msgs[0].value == -10


def test_network_port_url():
    assert to_serial_url("tcp://10.0.0.5:10001") == "socket://10.0.0.5:10001"
    assert to_serial_url("/dev/ttyUSB0") == "/dev/ttyUSB0"
    fake = MagicMock()
    with patch("serial.serial_for_url", return_value=fake) as open_url:
        port = _SerialPort(MagicMock(), "tcp://10.0.0.5:10001", MagicMock(), MagicMock())
        port.open()
    assert open_url.call_args[0][0] == "socket://10.0.0.5:10001"


# --------------------------------------------------------- switch events
async def test_switch_all_off_event(hass: HomeAssistant, network):
    entry = await _setup(hass)
    hub = entry.runtime_data
    await _wait(hass, lambda: hub.sync_count >= 1)
    # What the Kitchen switch sent for a long press: OFF for 2, 3, 1, 0
    network.port.emit_lighting(0x0B, bytes([0x01, 2, 0x01, 3, 0x01, 1, 0x01, 0]))
    await _wait(hass, lambda: _entity(hass, "event", "001004113837_unit_11"))
    eid = _entity(hass, "event", "001004113837_unit_11")
    await _wait(hass, lambda: hass.states.get(eid).attributes.get("event_type") == "all_off")
    state = hass.states.get(eid)
    assert state.attributes["groups"] == [2, 3, 1, 0]
    assert state.attributes["unit_address"] == 11
    assert hass.states.get(_light_ids(hass)[1]).state == STATE_OFF

    # a single press
    network.port.emit_lighting(0x0B, bytes([0x79, 5]))
    await _wait(hass, lambda: hass.states.get(eid).attributes.get("event_type") == "on")
    assert hass.states.get(eid).attributes["group"] == 5


async def test_scene_from_switch(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    network.port.emit_lighting(0x0C, bytes([0x0A, 0, 128, 0x01, 1]))
    await _wait(hass, lambda: _entity(hass, "event", "001004113837_unit_12"))
    eid = _entity(hass, "event", "001004113837_unit_12")
    await _wait(hass, lambda: hass.states.get(eid).attributes.get("event_type") == "scene")
    assert hass.states.get(eid).attributes["levels"] == [128, 0]


async def test_unit_names_from_project(hass: HomeAssistant, network, tmp_path):
    xml = tmp_path / "proj.xml"
    xml.write_text(
        "<Installation><Project><Network>"
        "<Unit><TagName>KITCHEN</TagName><Address>11</Address><UnitType>KEYB4</UnitType>"
        "<CatalogNumber>5084NL</CatalogNumber></Unit>"
        "<Unit><TagName>RELAY</TagName><Address>3</Address><UnitType>RELDN8</UnitType></Unit>"
        "</Network></Project></Installation>"
    )
    entry = await _setup(hass, project_file=str(xml))
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    # key units are created straight away, output units are not
    eid = _entity(hass, "event", "001004113837_unit_11")
    assert eid == "event.kitchen_presses"
    assert _entity(hass, "event", "001004113837_unit_3") is None


# --------------------------------------------------------------- scenes
async def test_trigger_events_and_service(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    network.port.emit_packet(bytes([0x05, 0x0B, 0xCA, 0x00, 0x02, 0x03, 0x07]))
    await _wait(hass, lambda: _entity(hass, "event", "001004113837_trigger_3"))
    eid = _entity(hass, "event", "001004113837_trigger_3")
    await _wait(hass, lambda: hass.states.get(eid).attributes.get("event_type") == "trigger")
    assert hass.states.get(eid).attributes["action_selector"] == 7

    await hass.services.async_call(
        DOMAIN, "trigger", {"group": 3, "action_selector": 2}, blocking=True
    )
    assert network.commands[-1] == bytes([0x05, 0xCA, 0x00, 0x02, 0x03, 0x02])


# -------------------------------------------------------------- sensors
async def test_sensors(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    network.port.emit_packet(bytes([0x05, 0x20, 0x19, 0x00, 0x02, 0x04, 0x5A]))
    network.port.emit_packet(
        bytes([0x05, 0x21, 0xE4, 0x00, 0x0E, 0x01, 0x02, 0x10, 0x00, 0x01, 0xF4])
    )
    await _wait(hass, lambda: _entity(hass, "sensor", "001004113837_temperature_4"))
    await _wait(hass, lambda: _entity(hass, "sensor", "001004113837_measurement_1_2"))
    temp = _entity(hass, "sensor", "001004113837_temperature_4")
    await _wait(hass, lambda: hass.states.get(temp).state == "22.5")
    assert hass.states.get(temp).attributes["unit_of_measurement"] == "°C"
    lux = hass.states.get(_entity(hass, "sensor", "001004113837_measurement_1_2"))
    assert lux.state == "500.0" and lux.attributes["unit_of_measurement"] == "lx"


# ---------------------------------------------------------- group types
async def test_group_types(hass: HomeAssistant, network):
    network.groups.update({7: 0, 8: 0})
    entry = await _setup(
        hass,
        group_types={"5": "cover", "3": "fan", "7": "switch", "8": "light_onoff"},
    )
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    lights = _light_ids(hass)
    assert 5 not in lights and 3 not in lights and 7 not in lights
    assert 8 in lights
    assert hass.states.get(lights[8]).attributes["supported_color_modes"] == ["onoff"]

    cover = _entity(hass, "cover", "001004113837_56_5")
    await hass.services.async_call("cover", "open_cover", {"entity_id": cover}, blocking=True)
    assert network.groups[5] == 255
    assert hass.states.get(cover).state == "open"
    await hass.services.async_call("cover", "stop_cover", {"entity_id": cover}, blocking=True)
    assert network.commands[-1][3:] == bytes([0x02, 5, 5])  # ramp to 2% = stop
    await hass.services.async_call(
        "cover", "set_cover_position", {"entity_id": cover, "position": 50}, blocking=True
    )
    assert network.groups[5] == 128
    assert hass.states.get(cover).attributes["current_position"] == 50
    # a wall plate's "stop" (level 4-6) is not a position
    network.port.emit_lighting(0x0B, bytes([0x02, 5, 4]))
    await hass.async_block_till_done()
    await _wait(hass, lambda: True)
    assert hass.states.get(cover).attributes["current_position"] == 50
    await hass.services.async_call("cover", "close_cover", {"entity_id": cover}, blocking=True)
    assert hass.states.get(cover).state == "closed"

    fan = _entity(hass, "fan", "001004113837_56_3")
    # 128 is half way between low (86) and medium (170)
    assert hass.states.get(fan).attributes["percentage"] in (33, 67)
    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": fan, "percentage": 30}, blocking=True
    )
    assert network.groups[3] == 86
    await hass.services.async_call("fan", "turn_off", {"entity_id": fan}, blocking=True)
    assert network.groups[3] == 0

    switch = _entity(hass, "switch", "001004113837_56_7")
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    assert network.groups[7] == 255
    assert hass.states.get(switch).state == STATE_ON


async def test_changing_type_removes_old_entity(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    assert 5 in _light_ids(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "group_types": {"5": "cover"}}
    )
    await hass.async_block_till_done()
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    assert 5 not in _light_ids(hass)
    assert _entity(hass, "cover", "001004113837_56_5")


async def test_options_group_types_flow(hass: HomeAssistant, network):
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "group_types"}
    )
    assert result["type"] is FlowResultType.FORM
    labels = [str(k) for k in result["data_schema"].schema]
    assert "5: C-Bus Group 5" in labels
    data = {label: "light" for label in labels}
    data["5: C-Bus Group 5"] = "cover"
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["group_types"] == {"5": "cover"}
    assert entry.options["timesync"] is False  # other options kept


# ------------------------------------------------------- removed groups
async def test_removed_group_stays_removed(hass: HomeAssistant, network):
    entry = await _setup(hass)
    hub = entry.runtime_data
    await _wait(hass, lambda: hub.sync_count >= 1)
    await hass.services.async_call(DOMAIN, "remove_group", {"group": 5}, blocking=True)
    network.port.emit_lighting(0x0B, bytes([0x79, 5]))  # seen on the network
    await hub.async_resync()  # and present on an output unit
    await hass.async_block_till_done()
    assert 5 not in _light_ids(hass)
    assert 5 not in hub.groups

    await hass.services.async_call(DOMAIN, "add_group", {"group": 5}, blocking=True)
    await _wait(hass, lambda: 5 in _light_ids(hass))


async def test_network_address_config_flow(hass: HomeAssistant, network):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"port": "tcp://192.168.1.50:10001"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"port": "tcp://192.168.1.50:10001"}
    await hass.async_block_till_done()
    hub = result["result"].runtime_data
    assert hub.is_network
    assert len(hub.pci._line_settings) == 1  # noqa: SLF001
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_options_project_upload(hass: HomeAssistant, network, tmp_path):
    from contextlib import contextmanager

    upload = tmp_path / "upload"
    upload.write_text(
        "<Installation><Project><Network>"
        "<Unit><TagName>KITCHEN</TagName><Address>11</Address><UnitType>KEYB4</UnitType></Unit>"
        "</Network></Project></Installation>"
    )

    @contextmanager
    def fake_upload(_hass, file_id):
        assert file_id == "0e0a6b4b-4a1c-4c2b-9f43-6a8f1c2d3e4f"
        yield upload

    hass.config.config_dir = str(tmp_path)
    entry = await _setup(hass)
    await _wait(hass, lambda: entry.runtime_data.sync_count >= 1)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    with patch("custom_components.cbus.config_flow.process_uploaded_file", fake_upload):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"project_upload": "0e0a6b4b-4a1c-4c2b-9f43-6a8f1c2d3e4f", "resync_minutes": 30, "timesync": False},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["project_file"] == str(tmp_path / "cbus" / "project.xml")
    await hass.async_block_till_done()
    await _wait(hass, lambda: _entity(hass, "event", "001004113837_unit_11"))
    assert _entity(hass, "event", "001004113837_unit_11") == "event.kitchen_presses"
