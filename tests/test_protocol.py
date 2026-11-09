"""Tests for the C-Bus protocol layer (no Home Assistant needed)."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from custom_components.cbus import protocol as p
from tests.fake_pci import FakeNetwork


# ---------------------------------------------------------------- codecs
def test_checksum_known_vector():
    # From the Serial Interface Guide / micolous tests: GA 100 on
    raw = bytes.fromhex("0538007964")
    framed = p.with_checksum(raw)
    assert framed[-1] == 0xE6
    assert sum(framed) % 256 == 0


def test_manchester_round_trip():
    assert p.manchester_decode(0xAA, 0xAA) == 0
    assert p.manchester_decode(0x55, 0x55) == 255
    assert p.manchester_decode(0x95, 0x99) == 0x57
    assert p.manchester_decode(0x00, 0x00) is None
    for v in (0, 1, 77, 128, 254, 255):
        b = p.manchester_encode(v)
        assert p.manchester_decode(b[0], b[1]) == v


def test_decode_real_binary_reply():
    # Serial Interface Guide s9.1, verified on hardware by micolous/cbus
    msgs = p.decode_line(
        b"86999900F9003800A8AA0200000000000000000000000000000000000000C3"
    )
    assert len(msgs) == 1
    rep = msgs[0]
    assert rep.kind == "binary" and rep.application == 0x38 and rep.block_start == 0
    assert len(rep.values) == 88
    for ga, v in enumerate(rep.values):
        assert v == (p.GROUP_OFF if 1 <= ga <= 8 else p.GROUP_MISSING)


def test_decode_level_reply():
    msgs = p.decode_line(
        b"86FFFF00F90738000000AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA00000000A4"
    )
    rep = msgs[0]
    assert rep.kind == "level" and rep.block_start == 0 and len(rep.values) == 11
    assert rep.values[0] is None
    assert rep.values[1:9] == (0,) * 8
    assert rep.values[9] is None


def test_decode_lighting_events():
    raw = bytes([0x05, 0x0B, 0x38, 0x00, 0x79, 0x01, 0x01, 0x02, 0x22, 0x03, 0x80, 0x09, 0x04])
    msgs = p.decode_line(p.with_checksum(raw).hex().upper().encode())
    assert [(m.group, m.action, m.level, m.ramp_seconds) for m in msgs] == [
        (1, "on", 255, 0),
        (2, "off", 0, 0),
        (3, "ramp", 0x80, 20),
        (4, "terminate", None, 0),
    ]
    assert all(m.source == 0x0B for m in msgs)


def test_decode_ramp_to_zero_is_level_zero():
    raw = bytes([0x05, 0x0D, 0x38, 0x00, 0x0A, 0x05, 0x00])
    (m,) = p.decode_line(p.with_checksum(raw).hex().encode().upper())
    assert m.action == "ramp" and m.level == 0


def test_decode_ignores_garbage():
    assert p.decode_line(b"A3210038") == []
    assert p.decode_line(b"hello") == []
    assert p.decode_line(b"0538007964FF") == []  # bad checksum


def test_ramp_code_selection():
    assert p.duration_to_ramp_code(0) == 0x02
    assert p.duration_to_ramp_code(1) == 0x02
    assert p.duration_to_ramp_code(3) == 0x0A
    assert p.duration_to_ramp_code(15) == 0x1A
    assert p.duration_to_ramp_code(5000) == 0x7A


def test_clock_sals():
    sals = p.clock_sals(datetime(2026, 11, 8, 23, 5, 9))
    assert sals[:6] == bytes([0x0D, 0x01, 23, 5, 9, 0xFF])
    assert sals[6:] == bytes([0x0E, 0x02, 0x07, 0xEA, 11, 8, 6])


# ---------------------------------------------------------------- client
async def _client(net: FakeNetwork, wait: float = 5, **kw) -> tuple[p.CBusPCI, list]:
    events: list = []
    pci = p.CBusPCI(
        "/dev/fake",
        on_lighting=events.append,
        port_factory=net.factory,
        timesync_interval=0,
        command_timeout=0.3,
        status_timeout=0.5,
        **kw,
    )
    await pci.start()
    assert await pci.wait_connected(wait)
    return pci, events


async def test_initialise_and_commands():
    net = FakeNetwork({0: 0, 1: 255, 3: 128})
    pci, _ = await _client(net)
    try:
        written = bytes(net.port.written)
        assert written.startswith(b"~\r~\r~\rA3210038\rA342000E\rA3300059\r")
        await pci.lighting_on(0)
        assert net.groups[0] == 255
        await pci.lighting_off(1)
        assert net.groups[1] == 0
        used = await pci.lighting_ramp(3, 40, 5)
        assert used == 4 and net.groups[3] == 40
    finally:
        await pci.stop()


async def test_status_requests():
    net = FakeNetwork({0: 0, 1: 255, 3: 128, 40: 10})
    pci, _ = await _client(net)
    try:
        states = await pci.binary_status()
        assert len(states) == 256
        assert states[0] == p.GROUP_OFF and states[1] == p.GROUP_ON
        assert states[2] == p.GROUP_MISSING and states[40] == p.GROUP_ON
        levels = await pci.level_status(0)
        assert levels[0] == 0 and levels[1] == 255 and levels[3] == 128
        assert levels[2] is None
        levels = await pci.level_status(32)
        assert levels[40] == 10 and len(levels) == 32
    finally:
        await pci.stop()


async def test_level_request_unsupported_times_out():
    net = FakeNetwork({0: 0})
    net.level_requests_supported = False
    pci, _ = await _client(net)
    try:
        assert await pci.level_status(0) == {}
    finally:
        await pci.stop()


async def test_retry_when_not_confirmed():
    net = FakeNetwork({5: 0})
    pci, _ = await _client(net)
    try:
        net.drop_confirmations = 1
        net.fail_confirmations = 1
        await pci.lighting_on(5)
        assert net.groups[5] == 255
        assert len([c for c in net.commands if c[1] == 0x38]) == 3
    finally:
        await pci.stop()


async def test_command_failure_raises():
    net = FakeNetwork({5: 0})
    pci, _ = await _client(net)
    try:
        net.drop_confirmations = 10
        with pytest.raises(p.CBusCommandFailed):
            await pci.lighting_on(5)
    finally:
        await pci.stop()


async def test_events_from_network():
    net = FakeNetwork({1: 0})
    pci, events = await _client(net)
    try:
        net.port.emit_lighting(0x0B, bytes([0x79, 0x01]))
        net.port.emit_lighting(0x0B, bytes([0x0A, 0x01, 0x00]))
        await asyncio.sleep(0.05)
        assert [(e.group, e.action, e.level) for e in events] == [
            (1, "on", 255),
            (1, "ramp", 0),
        ]
    finally:
        await pci.stop()


async def test_answers_clock_request():
    net = FakeNetwork({})
    pci, _ = await _client(net)
    try:
        net.port.emit_packet(bytes([0x05, 0x0C, 0xDF, 0x00, 0x11, 0x03]))
        await asyncio.sleep(0.1)
        assert any(c[1] == 0xDF for c in net.commands)
    finally:
        await pci.stop()


async def test_reconnect_after_disconnect():
    connects: list = []
    net = FakeNetwork({1: 0})
    pci = p.CBusPCI(
        "/dev/fake",
        port_factory=net.factory,
        on_connected=lambda: connects.append(True),
        on_disconnected=lambda: connects.append(False),
        timesync_interval=0,
        command_timeout=0.3,
    )
    await pci.start()
    try:
        assert await pci.wait_connected(5)
        net.port.disconnect()
        await asyncio.sleep(0.05)
        assert not pci.connected
        with pytest.raises(p.CBusNotConnected):
            await pci.lighting_on(1)
        # runner backs off 2s then reconnects
        assert await pci.wait_connected(6)
        assert connects == [True, False, True]
        await pci.lighting_on(1)
        assert net.groups[1] == 255
    finally:
        await pci.stop()


async def test_open_failure_retries():
    net = FakeNetwork({})
    net.open_error = OSError("busy")
    pci = p.CBusPCI("/dev/fake", port_factory=net.factory, timesync_interval=0)
    await pci.start()
    try:
        await asyncio.sleep(0.1)
        assert not pci.connected and "busy" in pci.last_error
        net.open_error = None
        assert await pci.wait_connected(6)
    finally:
        await pci.stop()


async def test_detects_baud_rate():
    net = FakeNetwork({1: 0})
    net.baudrate = 38400
    pci, _ = await _client(net, wait=60, baudrates=(9600, 19200, 38400), line_settings=((True, True),))
    try:
        assert pci.baudrate == 38400
        await pci.lighting_on(1)
        assert net.groups[1] == 255
    finally:
        await pci.stop()


async def test_pac_programming_mode_detected_and_waits():
    net = FakeNetwork({})
    statuses = []

    class PacPort:
        def __init__(self, loop, port, on_data, on_lost):
            self.loop, self.on_data = loop, on_data

        def open(self):
            pass

        def start_reading(self):
            pass

        def close(self):
            pass

        def write(self, data):
            self.loop.call_later(0.05, self.on_data, p.PAC_PROGRAMMING_REPLY)

    pci = p.CBusPCI(
        "/dev/does-not-exist",
        port_factory=PacPort,
        timesync_interval=0,
        on_status=lambda: statuses.append(True),
    )
    await pci.start()
    try:
        for _ in range(100):
            if pci.pac_programming_mode:
                break
            await asyncio.sleep(0.1)
        assert pci.pac_programming_mode
        assert "programming mode" in pci.last_error
        assert statuses
        assert not pci.connected
    finally:
        await pci.stop()
