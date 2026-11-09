"""A simulated C-Bus PCI and network, for tests."""
from __future__ import annotations

import asyncio
from base64 import b16decode, b16encode

from custom_components.cbus.protocol import (
    RAMP_CODE_TO_SECONDS,
    checksum,
    manchester_encode,
    with_checksum,
)


class FakeNetwork:
    """State shared between the fake PCI and the test."""

    def __init__(self, groups: dict[int, int] | None = None) -> None:
        # group address -> level, for groups that exist on output units
        self.groups: dict[int, int] = dict(groups or {})
        self.level_requests_supported = True
        self.drop_confirmations = 0  # number of commands to not confirm
        self.fail_confirmations = 0  # number of commands to confirm with '#'
        self.commands: list[bytes] = []  # raw decoded packets received
        self.ports: list[FakePort] = []
        self.open_error: Exception | None = None
        self.baudrate = 9600  # speed the fake PCI talks at

    @property
    def port(self) -> FakePort:
        return self.ports[-1]

    def factory(self, loop, port, on_data, on_lost):
        p = FakePort(self, loop, on_data, on_lost)
        self.ports.append(p)
        return p


class FakePort:
    """Stands in for the serial port; behaves like a PCI in SMART mode."""

    def __init__(self, net: FakeNetwork, loop, on_data, on_lost) -> None:
        self.net = net
        self.loop = loop
        self.on_data = on_data
        self.on_lost = on_lost
        self.rx = bytearray()
        self.smart = False
        self.closed = False
        self.written = bytearray()
        self.baudrate = 9600

    def set_baudrate(self, baudrate: int) -> None:
        self.baudrate = baudrate

    # --- port API
    def open(self) -> None:
        if self.net.open_error:
            raise self.net.open_error

    def start_reading(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        if self.baudrate != self.net.baudrate:
            self.send(b"\xff\xf0\x00")  # garbage, as at the wrong speed
            return
        self.written += data
        self.rx += data
        while b"\r" in self.rx:
            end = self.rx.index(b"\r")
            line = bytes(self.rx[:end])
            del self.rx[: end + 1]
            self._line(line)

    # --- helpers for tests
    def send(self, data: bytes) -> None:
        """Send bytes to the client."""
        if not self.closed:
            self.loop.call_soon(self.on_data, data)

    def disconnect(self) -> None:
        self.closed = True
        self.loop.call_soon(self.on_lost, OSError("unplugged"))

    def emit_packet(self, raw: bytes) -> None:
        self.send(b16encode(with_checksum(raw)) + b"\r\n")

    def emit_lighting(self, source: int, sals: bytes, app: int = 0x38) -> None:
        """A unit on the network sends lighting SALs."""
        raw = bytes([0x05, source, app, 0x00]) + sals
        self._apply_lighting(raw[4:])
        self.emit_packet(raw)

    # --- PCI behaviour
    def _line(self, line: bytes) -> None:
        if line.startswith(b"~"):
            self.smart = False
            return
        if not self.smart:
            # basic mode echoes what it receives
            self.send(line + b"\r\n")
            if line == b"A3300059":
                self.smart = True
            return
        if not line.startswith(b"\\"):
            return
        body = line[1:]
        code = None
        if body and body[-1:] not in b"0123456789ABCDEF":
            code = body[-1:]
            body = body[:-1]
        raw = b16decode(body)
        assert checksum(raw[:-1]) == raw[-1], f"bad checksum from client {line}"
        raw = raw[:-1]
        self.net.commands.append(raw)
        if self.net.drop_confirmations:
            self.net.drop_confirmations -= 1
            return
        if self.net.fail_confirmations:
            self.net.fail_confirmations -= 1
            if code:
                self.send(code + b"#")
            return
        if code:
            self.send(code + b".")
        if raw[0] == 0x05:
            app = raw[1]
            sals = raw[3:]
            if app == 0x38:
                self._apply_lighting(sals)
            elif app == 0xFF:
                self._status_request(sals)

    def _apply_lighting(self, sals: bytes) -> None:
        i = 0
        while i + 1 < len(sals):
            cmd, group = sals[i], sals[i + 1]
            if cmd == 0x79:
                self.net.groups[group] = 255 if group in self.net.groups else 255
                i += 2
            elif cmd == 0x01:
                if group in self.net.groups:
                    self.net.groups[group] = 0
                i += 2
            elif cmd == 0x09:
                i += 2
            elif cmd in RAMP_CODE_TO_SECONDS:
                if group in self.net.groups:
                    self.net.groups[group] = sals[i + 2]
                i += 3
            else:
                break

    def _status_request(self, sals: bytes) -> None:
        if sals[0] == 0x7A:
            app, block = sals[1], sals[2]
            self._binary_reply(app, block)
        elif sals[:2] == b"\x73\x07":
            app, block = sals[2], sals[3]
            if self.net.level_requests_supported:
                self._level_reply(app, block)

    def _reply(self, coding: int, app: int, start: int, payload: bytes) -> None:
        body = bytes([coding, app, start]) + payload
        raw = bytes([0x86, 0x99, 0x99, 0x00, 0xE0 | len(body)]) + body
        self.emit_packet(raw)

    def _binary_reply(self, app: int, block: int) -> None:
        def state(group: int) -> int:
            if group not in self.net.groups:
                return 0
            return 1 if self.net.groups[group] > 0 else 2

        start = block
        while start < 256:
            count = min(88, 256 - start)
            payload = bytearray()
            for g in range(start, start + count, 4):
                b = 0
                for k in range(4):
                    if g + k < 256:
                        b |= state(g + k) << (2 * k)
                payload.append(b)
            self._reply(0x00, app, start, bytes(payload))
            start += count

    def _level_reply(self, app: int, block: int) -> None:
        start = block
        end = block + 32
        while start < end:
            count = min(11, end - start)
            payload = b"".join(
                manchester_encode(self.net.groups.get(g)) for g in range(start, start + count)
            )
            self._reply(0x07, app, start, payload)
            start += count


async def settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)
