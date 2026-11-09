"""C-Bus PC Interface (PCI / PAC) serial protocol.

This module has no Home Assistant dependencies so it can be tested on its own.

It talks to a Clipsal C-Bus PC Interface (5500PC, 5500PCU, 5500PACA, ...) in
SMART mode over a serial port, and provides:

* lighting commands (on, off, ramp to level, terminate ramp)
* an acknowledged command queue (every command carries a confirmation code and
  is retried if the PCI does not confirm it)
* binary and level status requests, used to read the real state of every
  group address on the network
* decoding of lighting events from the network (wall switches, timers, other
  controllers)
* answering network clock requests and optional periodic time broadcasts
* automatic reconnection, and re-initialisation when the PCI power-cycles

Protocol references: Clipsal "C-Bus Serial Interface User Guide" and
"C-Bus Lighting Application" documents. The packet layouts and test vectors
were cross-checked against micolous/cbus (LGPLv3), which has been verified
against real hardware.
"""
from __future__ import annotations

import asyncio
from base64 import b16decode, b16encode
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

# --- Applications ---------------------------------------------------------
APP_LIGHTING = 0x38
APP_TEMPERATURE = 0x19
APP_TRIGGER = 0xCA
APP_MEASUREMENT = 0xE4
APP_CLOCK = 0xDF
APP_STATUS_REQUEST = 0xFF
LIGHTING_APPLICATIONS = range(0x30, 0x60)

# --- Lighting commands ----------------------------------------------------
CMD_OFF = 0x01
CMD_ON = 0x79
CMD_TERMINATE_RAMP = 0x09

# --- Trigger Control commands (same layout as lighting) -------------------
TRIGGER_MIN = 0x01  # action selector 0
TRIGGER_MAX = 0x79  # action selector 255
TRIGGER_EVENT = 0x02  # group, action selector
TRIGGER_INDICATOR_KILL = 0x09

# --- Temperature Broadcast / Measurement ----------------------------------
TEMPERATURE_BROADCAST = 0x02  # group, temperature * 4
MEASUREMENT_EVENT = 0x0E  # device, channel, unit, exponent, value (int16)

# Ramp command codes and their durations in seconds.
RAMP_RATES: tuple[tuple[int, int], ...] = (
    (0x02, 0),
    (0x0A, 4),
    (0x12, 8),
    (0x1A, 12),
    (0x22, 20),
    (0x2A, 30),
    (0x32, 40),
    (0x3A, 60),
    (0x42, 90),
    (0x4A, 120),
    (0x52, 180),
    (0x5A, 300),
    (0x62, 420),
    (0x6A, 600),
    (0x72, 900),
    (0x7A, 1020),
)
RAMP_CODE_TO_SECONDS = dict(RAMP_RATES)

# --- Status replies -------------------------------------------------------
GROUP_MISSING = 0
GROUP_ON = 1
GROUP_OFF = 2
GROUP_ERROR = 3

# --- Serial framing -------------------------------------------------------
CONFIRMATION_CODES = b"hijklmnopqrstuvwxyzg"
CONFIRM_OK = ord(".")
CONFIRM_FAILURES = {
    ord("#"): "too many retransmissions",
    ord("$"): "transmission corrupted",
    ord("%"): "loss of synchronisation",
    ord("'"): "message too long",
}
HEX_CHARS = frozenset(b"0123456789ABCDEF")

# PCI initialisation (Serial Interface User Guide s10.2). These are the same
# settings cmqttd uses, which are known to work with this hardware.
INIT_SEQUENCE: tuple[bytes, ...] = (
    b"~\r",
    b"~\r",
    b"~\r",
    # Application address 1 = lighting (0x38)
    b"A3210038\r",
    # Interface options #3 = LOCAL_SAL | PUN | EXSTAT
    b"A342000E\r",
    # Interface options #1 = CONNECT | SRCHK | SMART | IDMON
    b"A3300059\r",
)

_MANCHESTER = (0xA, 0x9, 0x6, 0x5)


class CBusError(Exception):
    """Base error."""


class CBusNotConnected(CBusError):
    """The PCI is not connected."""


class CBusCommandFailed(CBusError):
    """The PCI did not confirm a command."""


class CBusPacProgrammingMode(CBusError):
    """A Pascal Automation Controller is answering in its programming protocol.

    It only switches to PC Interface mode after its USB cable is unplugged and
    plugged back in.
    """


# Reply a 5500PACA sends (in its PICED programming protocol) to anything it
# does not understand: sync, 0xF0, length 0x0006, error 0xFE, checksum.
PAC_PROGRAMMING_REPLY = b"\xff\xf0\x00\x06\xfe\x0b"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def checksum(data: bytes) -> int:
    """C-Bus checksum: two's complement of the byte sum."""
    return (-sum(data)) & 0xFF


def with_checksum(data: bytes) -> bytes:
    """Append a checksum."""
    return data + bytes([checksum(data)])


def duration_to_ramp_code(seconds: float) -> int:
    """Return the ramp command code closest to the given duration."""
    seconds = max(0.0, float(seconds))
    return min(RAMP_RATES, key=lambda rate: abs(rate[1] - seconds))[0]


def manchester_decode(b0: int, b1: int) -> int | None:
    """Decode a level from a level status report (2 bytes per level)."""
    try:
        n0 = _MANCHESTER.index(b0 & 0xF)
        n1 = _MANCHESTER.index(b0 >> 4)
        n2 = _MANCHESTER.index(b1 & 0xF)
        n3 = _MANCHESTER.index(b1 >> 4)
    except ValueError:
        return None
    return n0 | n1 << 2 | n2 << 4 | n3 << 6


def manchester_encode(value: int | None) -> bytes:
    """Encode a level the way a level status report does (for tests)."""
    if value is None:
        return b"\x00\x00"
    value &= 0xFF
    return bytes(
        [
            _MANCHESTER[value & 3] | _MANCHESTER[value >> 2 & 3] << 4,
            _MANCHESTER[value >> 4 & 3] | _MANCHESTER[value >> 6 & 3] << 4,
        ]
    )


def encode_point_to_multipoint(application: int, sals: bytes) -> bytes:
    """Build a point-to-multipoint packet (raw bytes, with checksum)."""
    return with_checksum(bytes([0x05, application, 0x00]) + sals)


def lighting_sal(command: int, group: int, level: int | None = None) -> bytes:
    """Build a single lighting SAL."""
    if not 0 <= group <= 255:
        raise ValueError(f"group address out of range: {group}")
    if level is None:
        return bytes([command, group])
    return bytes([command, group, max(0, min(255, int(level)))])


def status_request_sal(application: int, block: int, level: bool) -> bytes:
    """Build a status request SAL (binary or level)."""
    block &= 0xE0
    if level:
        return bytes([0x73, 0x07, application, block])
    return bytes([0x7A, application, block])


def trigger_sal(group: int, action_selector: int) -> bytes:
    """Build a Trigger Control SAL."""
    if not 0 <= group <= 254:
        raise ValueError(f"trigger group out of range: {group}")
    action_selector = max(0, min(255, int(action_selector)))
    return bytes([TRIGGER_EVENT, group, action_selector])


def clock_sals(when: datetime) -> bytes:
    """Build clock application SALs for time and date."""
    time_sal = bytes([0x0D, 0x01, when.hour, when.minute, when.second, 0xFF])
    date_sal = bytes(
        [
            0x0E,
            0x02,
            (when.year >> 8) & 0xFF,
            when.year & 0xFF,
            when.month,
            when.day,
            when.weekday(),
        ]
    )
    return time_sal + date_sal


# --------------------------------------------------------------------------
# Decoded messages
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LightingEvent:
    """A lighting command seen on the network."""

    source: int | None
    application: int
    group: int
    action: str  # "on", "off", "ramp", "terminate"
    level: int | None
    ramp_seconds: int = 0


@dataclass(frozen=True)
class StatusReport:
    """Part of a reply to a status request."""

    application: int
    block_start: int
    kind: str  # "binary" or "level"
    values: tuple[Any, ...]


@dataclass(frozen=True)
class ClockRequest:
    """A unit asked for the network time."""

    source: int | None


@dataclass(frozen=True)
class TriggerEvent:
    """A Trigger Control (scene) command seen on the network."""

    source: int | None
    group: int
    action_selector: int | None  # None for "indicator kill"
    action: str = "trigger"  # "trigger" or "indicator_kill"


@dataclass(frozen=True)
class TemperatureReading:
    """A Temperature Broadcast application reading."""

    source: int | None
    group: int
    celsius: float


@dataclass(frozen=True)
class MeasurementReading:
    """A Measurement application reading."""

    source: int | None
    device: int
    channel: int
    unit_code: int
    value: float


def _decode_lighting_sals(source: int | None, app: int, data: bytes) -> list:
    out: list[LightingEvent] = []
    i = 0
    while i + 1 < len(data):
        cmd = data[i]
        group = data[i + 1]
        if cmd >= 0x80:
            # long form (labels etc): length in low 5 bits, after the group
            i += 2 + (cmd & 0x1F)
            continue
        length = cmd & 0x07  # bytes after the command byte
        if length < 1 or i + 1 + length > len(data):
            break
        if cmd == CMD_ON:
            out.append(LightingEvent(source, app, group, "on", 255))
        elif cmd == CMD_OFF:
            out.append(LightingEvent(source, app, group, "off", 0))
        elif cmd == CMD_TERMINATE_RAMP:
            out.append(LightingEvent(source, app, group, "terminate", None))
        elif cmd in RAMP_CODE_TO_SECONDS and length == 2:
            level = data[i + 2]
            out.append(
                LightingEvent(
                    source,
                    app,
                    group,
                    "ramp",
                    level,
                    RAMP_CODE_TO_SECONDS[cmd],
                )
            )
        i += 1 + length
    return out


def _iter_sals(data: bytes):
    """Yield (command, args) for short-form SALs; skip long-form ones."""
    i = 0
    while i < len(data):
        cmd = data[i]
        if cmd >= 0x80:
            # long form (labels etc): length in low 5 bits, after the group
            i += 2 + (cmd & 0x1F)
            continue
        length = cmd & 0x07
        if length < 1 or i + 1 + length > len(data):
            return
        yield cmd, data[i + 1 : i + 1 + length]
        i += 1 + length


def _decode_trigger_sals(source: int | None, data: bytes) -> list:
    out: list[TriggerEvent] = []
    for cmd, args in _iter_sals(data):
        group = args[0]
        if cmd == TRIGGER_EVENT and len(args) == 2:
            out.append(TriggerEvent(source, group, args[1]))
        elif cmd == TRIGGER_MIN:
            out.append(TriggerEvent(source, group, 0))
        elif cmd == TRIGGER_MAX:
            out.append(TriggerEvent(source, group, 255))
        elif cmd == TRIGGER_INDICATOR_KILL:
            out.append(TriggerEvent(source, group, None, "indicator_kill"))
    return out


def _decode_temperature_sals(source: int | None, data: bytes) -> list:
    out: list[TemperatureReading] = []
    for cmd, args in _iter_sals(data):
        if cmd == TEMPERATURE_BROADCAST and len(args) == 2:
            out.append(TemperatureReading(source, args[0], args[1] / 4))
    return out


def _decode_measurement_sals(source: int | None, data: bytes) -> list:
    out: list[MeasurementReading] = []
    for cmd, args in _iter_sals(data):
        if cmd == MEASUREMENT_EVENT and len(args) == 6:
            device, channel, unit_code, exponent = args[0], args[1], args[2], args[3]
            if exponent >= 0x80:
                exponent -= 0x100
            raw = int.from_bytes(args[4:6], "big", signed=True)
            value = round(raw * 10.0**exponent, 6)
            out.append(MeasurementReading(source, device, channel, unit_code, value))
    return out


def _decode_clock_sals(source: int | None, data: bytes) -> list:
    out: list[ClockRequest] = []
    i = 0
    while i < len(data):
        cmd = data[i]
        length = cmd & 0x07
        if cmd == 0x11 and i + 1 < len(data) and data[i + 1] == 0x03:
            out.append(ClockRequest(source))
        if cmd & 0xE0:
            break
        i += 1 + length
    return out


def _decode_status_cals(data: bytes) -> list:
    out: list[StatusReport] = []
    i = 0
    while i < len(data):
        cmd = data[i]
        if cmd & 0xE0 != 0xE0:
            # Standard status replies carry no addressing; we enable EXSTAT so
            # we should never see them. Anything else we do not need.
            break
        end = i + (cmd & 0x1F) + 1
        if end > len(data):
            break
        body = data[i + 1 : end]
        i = end
        if len(body) < 3:
            continue
        coding, app, block = body[0], body[1], body[2]
        payload = body[3:]
        block_type = coding & 0x07
        if block_type == 0x00:
            values = []
            for byte in payload:
                values += [byte & 3, byte >> 2 & 3, byte >> 4 & 3, byte >> 6 & 3]
            out.append(StatusReport(app, block, "binary", tuple(values)))
        elif block_type == 0x07 and len(payload) % 2 == 0:
            levels = tuple(
                manchester_decode(payload[j], payload[j + 1])
                for j in range(0, len(payload), 2)
            )
            out.append(StatusReport(app, block, "level", levels))
    return out


def decode_line(line: bytes) -> list:
    """Decode one CR/LF terminated line from the PCI (SMART mode).

    Returns a list of LightingEvent / StatusReport / ClockRequest. Lines that
    are not valid packets (eg: echoes during initialisation) return [].
    """
    line = line.strip()
    if len(line) < 4 or len(line) % 2 or not set(line) <= HEX_CHARS:
        return []
    raw = b16decode(line)
    if checksum(raw[:-1]) != raw[-1]:
        _LOGGER.debug("Bad checksum, ignoring: %s", line)
        return []
    raw = raw[:-1]
    flags = raw[0]
    if flags & 0x20:
        return []  # device management
    address_type = flags & 0x07
    if len(raw) < 3:
        return []
    source = raw[1]
    if address_type == 0x05:  # point to multipoint
        if len(raw) < 4:
            return []
        app, routing = raw[2], raw[3]
        if routing != 0:
            return []
        sals = raw[4:]
        if app in LIGHTING_APPLICATIONS:
            return _decode_lighting_sals(source, app, sals)
        if app == APP_TRIGGER:
            return _decode_trigger_sals(source, sals)
        if app == APP_TEMPERATURE:
            return _decode_temperature_sals(source, sals)
        if app == APP_MEASUREMENT:
            return _decode_measurement_sals(source, sals)
        if app == APP_CLOCK:
            return _decode_clock_sals(source, sals)
        return []
    if address_type == 0x06:  # point to point (status replies)
        if len(raw) < 5 or raw[3] != 0x00:
            return []  # bridged replies not supported
        return _decode_status_cals(raw[4:])
    return []


# --------------------------------------------------------------------------
# Serial transport
# --------------------------------------------------------------------------
def is_network_port(port: str) -> bool:
    """Whether a port is a network address rather than a serial device."""
    return "://" in port


def to_serial_url(port: str) -> str:
    """Turn tcp://host:port (or host:port) into a pyserial URL."""
    if port.startswith("tcp://"):
        return "socket://" + port[len("tcp://") :]
    return port


class _SerialPort:
    """Serial port with a background reader thread.

    A thread doing blocking reads is used rather than event-loop fd polling:
    it behaves the same on every platform and USB serial driver.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        port: str,
        on_data: Callable[[bytes], None],
        on_lost: Callable[[Exception | None], None],
    ) -> None:
        self._loop = loop
        self._port = port
        self._on_data = on_data
        self._on_lost = on_lost
        self._serial: Any = None
        self._thread: threading.Thread | None = None
        self._closing = False

    def set_lines(self, dtr: bool, rts: bool) -> None:
        """Set the DTR / RTS handshake lines."""
        if self._serial is not None:
            self._serial.dtr = dtr
            self._serial.rts = rts

    def set_baudrate(self, baudrate: int) -> None:
        """Change the port speed."""
        if self._serial is not None:
            self._serial.baudrate = baudrate
            try:
                self._serial.reset_input_buffer()
            except Exception:  # noqa: BLE001
                pass

    def open(self) -> None:
        """Open the port. Blocking - call from an executor.

        ``self._port`` is a device path, or a URL such as
        ``socket://192.168.1.50:10001`` for an Ethernet interface (5500CN).
        """
        import serial  # pylint: disable=import-outside-toplevel

        self._serial = serial.serial_for_url(
            to_serial_url(self._port),
            baudrate=9600,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.1,
            write_timeout=2,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )
        try:
            _LOGGER.debug(
                "Port %s open: baud=%s cts=%s dsr=%s cd=%s in_waiting=%s",
                self._port,
                self._serial.baudrate,
                self._serial.cts,
                self._serial.dsr,
                self._serial.cd,
                self._serial.in_waiting,
            )
        except Exception:  # noqa: BLE001
            pass

    def start_reading(self) -> None:
        """Start the reader thread (call from the loop)."""
        self._closing = False
        self._thread = threading.Thread(
            target=self._reader, name="cbus-serial-reader", daemon=True
        )
        self._thread.start()

    def _reader(self) -> None:
        ser = self._serial
        while not self._closing:
            try:
                data = ser.read(max(1, min(1024, ser.in_waiting)))
            except Exception as err:  # noqa: BLE001
                if not self._closing:
                    self._loop.call_soon_threadsafe(self._lost, err)
                return
            if data:
                self._loop.call_soon_threadsafe(self._on_data, data)

    def _lost(self, err: Exception | None) -> None:
        if self._closing:
            return
        self.close()
        self._on_lost(err)

    def write(self, data: bytes) -> None:
        if self._serial is None:
            raise CBusNotConnected
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as err:  # noqa: BLE001
            self._lost(err)
            raise CBusNotConnected from err

    def close(self) -> None:
        self._closing = True
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None


# --------------------------------------------------------------------------
# PCI client
# --------------------------------------------------------------------------
@dataclass
class _Collector:
    kind: str
    application: int
    start: int
    count: int
    values: dict[int, Any] = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, report: StatusReport) -> None:
        if report.kind != self.kind or report.application != self.application:
            return
        for offset, value in enumerate(report.values):
            group = report.block_start + offset
            if self.start <= group < self.start + self.count:
                self.values[group] = value
        if len(self.values) >= self.count:
            self.done.set()


class CBusPCI:
    """Asynchronous client for a C-Bus PC Interface."""

    def __init__(
        self,
        port: str,
        *,
        on_lighting: Callable[[LightingEvent], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        on_status: Callable[[], None] | None = None,
        on_packet: Callable[[list], None] | None = None,
        timesync_interval: float = 300,
        answer_clock_requests: bool = True,
        command_timeout: float = 2.0,
        command_retries: int = 3,
        status_timeout: float = 4.0,
        port_factory: Callable[..., Any] | None = None,
        baudrates: tuple[int, ...] = (9600,),
        line_settings: tuple[tuple[bool, bool], ...] = (
            (True, False),
            (False, True),
            (True, True),
        ),
    ) -> None:
        self.port = port
        self._baudrates = list(baudrates)
        self._line_settings = list(line_settings)
        self.baudrate: int | None = None
        self.lines: tuple[bool, bool] | None = None
        self._on_lighting = on_lighting
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_status = on_status
        self._on_packet = on_packet
        self._timesync_interval = timesync_interval
        self._answer_clock = answer_clock_requests
        self._command_timeout = command_timeout
        self._command_retries = command_retries
        self._status_timeout = status_timeout
        self._port_factory = port_factory or _SerialPort
        if is_network_port(port):
            # DTR / RTS mean nothing on a network connection.
            self._line_settings = [self._line_settings[0]]

        self._loop: asyncio.AbstractEventLoop | None = None
        self._serial: Any = None
        self._buffer = bytearray()
        self._send_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[bool]] = {}
        self._next_code = 0
        self._collectors: list[_Collector] = []
        self._connected = False
        self._lost = asyncio.Event()
        self._stopping = False
        self._runner: asyncio.Task | None = None
        self._timesync_task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._consecutive_failures = 0
        self.last_error: str | None = None
        self.level_requests_supported: bool | None = None
        self._last_init = 0.0
        self._raw_rx = b""
        self.pac_programming_mode = False

    # ---------------------------------------------------------------- state
    @property
    def connected(self) -> bool:
        return self._connected

    # -------------------------------------------------------------- control
    async def start(self) -> None:
        """Start connecting (and keep reconnecting) in the background."""
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._runner = self._loop.create_task(self._run(), name="cbus-pci")
        if self._timesync_interval:
            self._timesync_task = self._loop.create_task(
                self._timesync(), name="cbus-timesync"
            )

    async def stop(self) -> None:
        """Disconnect and stop."""
        self._stopping = True
        self._lost.set()
        for task in (self._runner, self._timesync_task, *self._background):
            if task:
                task.cancel()
        for task in (self._runner, self._timesync_task, *self._background):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._close()

    async def wait_connected(self, timeout: float) -> bool:
        """Wait until connected (or timeout)."""
        end = asyncio.get_running_loop().time() + timeout
        while not self._connected:
            if asyncio.get_running_loop().time() > end:
                return False
            await asyncio.sleep(0.05)
        return True

    def _spawn(self, coro) -> None:
        task = self._loop.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _run(self) -> None:
        backoff = 2.0
        while not self._stopping:
            try:
                await self._open()
                await self._initialise()
                self._consecutive_failures = 0
                if self.pac_programming_mode:
                    self.pac_programming_mode = False
                    self._notify_status()
                self._set_connected(True)
                backoff = 2.0
                await self._lost.wait()
            except asyncio.CancelledError:
                raise
            except CBusPacProgrammingMode as err:
                self.last_error = str(err)
                _LOGGER.error("C-Bus interface %s: %s", self.port, err)
                self._close()
                self._set_connected(False)
                self._notify_status()
                # Don't keep sending to the PAC; wait for it to be replugged.
                await self._wait_for_replug()
                continue
            except Exception as err:  # noqa: BLE001
                self.last_error = str(err) or type(err).__name__
                _LOGGER.warning(
                    "C-Bus interface %s unavailable: %s (retrying in %ss)",
                    self.port,
                    self.last_error,
                    int(backoff),
                )
            finally:
                self._close()
                self._set_connected(False)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _device_identity(self) -> tuple[str, int] | None:
        import os  # pylint: disable=import-outside-toplevel

        try:
            real = os.path.realpath(self.port)
            return real, os.stat(real).st_ino
        except OSError:
            return None

    async def _wait_for_replug(self, timeout: float = 1800) -> None:
        """Wait until the USB device disappears and comes back (or timeout)."""
        start = self._device_identity()
        gone = False
        end = self._loop.time() + timeout
        while not self._stopping and self._loop.time() < end:
            await asyncio.sleep(2)
            now = self._device_identity()
            if now is None:
                gone = True
            elif gone or now != start:
                _LOGGER.info("C-Bus interface %s was replugged", self.port)
                await asyncio.sleep(3)  # let the PAC boot
                return

    async def _open(self) -> None:
        self._lost.clear()
        self._buffer.clear()
        self._raw_rx = b""
        port = self._port_factory(
            self._loop, self.port, self._data_received, self._connection_lost
        )
        await self._loop.run_in_executor(None, port.open)
        self._serial = port
        port.start_reading()
        _LOGGER.debug("Opened %s", self.port)

    async def _initialise(self) -> None:
        """Reset the PCI, put it into SMART mode and check it answers.

        If the interface does not confirm a probe command, other serial speeds
        are tried, and the one that works is remembered.
        """
        candidates = list(self._baudrates)
        if self.baudrate in candidates:
            candidates.remove(self.baudrate)
            candidates.insert(0, self.baudrate)
        results = []
        combos = list(self._line_settings)
        if self.lines in combos:
            combos.remove(self.lines)
            combos.insert(0, self.lines)
        for baud in candidates:
            if hasattr(self._serial, "set_baudrate"):
                self._serial.set_baudrate(baud)
            for lines in combos:
                if hasattr(self._serial, "set_lines"):
                    self._serial.set_lines(*lines)
                for strategy in ("burst", "wake"):
                    await self._send_init_sequence(strategy)
                    ok = await self._probe()
                    label = f"{baud}/dtr={int(lines[0])},rts={int(lines[1])}/{strategy}"
                    results.append(f"{label}={'ok' if ok else 'no'}")
                    if ok:
                        if (self.baudrate, self.lines) != (baud, lines):
                            _LOGGER.info(
                                "C-Bus interface %s answering at %s", self.port, label
                            )
                        self.baudrate = baud
                        self.lines = lines
                        self._last_init = self._loop.time()
                        _LOGGER.info("C-Bus interface %s initialised", self.port)
                        return
                    _LOGGER.debug("No reply from %s at %s", self.port, label)
                    if PAC_PROGRAMMING_REPLY in self._raw_rx:
                        self.pac_programming_mode = True
                        raise CBusPacProgrammingMode(
                            "the Pascal Automation Controller is in programming "
                            "mode; unplug its USB cable and plug it back in"
                        )
            if len(candidates) == 1:
                break
        raise CBusNotConnected(
            "interface did not respond (" + ", ".join(results) + ")"
        )

    async def _send_init_sequence(self, strategy: str = "burst") -> None:
        """Reset the interface into SMART mode.

        The whole sequence is written in one go, as C-Gate and cmqttd do. A
        Pascal Automation Controller (5500PACA) only switches from its own
        programming protocol to PC Interface mode when it sees the reset
        characters together; "wake" additionally gives it time to switch
        before the configuration commands are sent.
        """
        if strategy == "wake":
            self._write(b"~~~\r")
            await asyncio.sleep(3.0)
        self._write(b"".join(INIT_SEQUENCE))
        await asyncio.sleep(1.0)
        # Anything received so far is echo from basic mode.
        self._buffer.clear()

    async def _probe(self) -> bool:
        """Send a harmless command and wait for its confirmation."""
        packet = encode_point_to_multipoint(
            APP_STATUS_REQUEST, status_request_sal(APP_LIGHTING, 0, level=False)
        )
        code = self._confirmation_code()
        fut: asyncio.Future[bool] = self._loop.create_future()
        self._pending[code] = fut
        try:
            self._write(b"\\" + b16encode(packet) + bytes([code]) + b"\r")
            return await asyncio.wait_for(fut, 2.0)
        except TimeoutError:
            return False
        finally:
            self._pending.pop(code, None)
            await asyncio.sleep(0.8)  # let any status reply drain

    def _close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(False)
        self._pending.clear()

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        callback = self._on_connected if connected else self._on_disconnected
        if callback:
            try:
                callback()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in connection callback")

    def _notify_status(self) -> None:
        if self._on_status:
            try:
                self._on_status()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in status callback")

    def _connection_lost(self, err: Exception | None) -> None:
        self.last_error = str(err) if err else "connection closed"
        _LOGGER.warning("Lost connection to %s: %s", self.port, self.last_error)
        self._serial = None
        self._lost.set()

    def force_reconnect(self) -> None:
        """Drop the connection; the runner will reconnect."""
        self._lost.set()

    # ------------------------------------------------------------- receive
    def _data_received(self, data: bytes) -> None:
        _LOGGER.debug("RX %r", data)
        self._raw_rx = (self._raw_rx + data)[-64:]
        self._buffer += data
        buf = self._buffer
        while buf:
            first = buf[0]
            if first in b"\r\n ":
                del buf[0]
                continue
            if first == ord("+"):
                del buf[0]
                if self._loop.time() - self._last_init > 5:
                    _LOGGER.info(
                        "C-Bus interface reported power-up; re-initialising"
                    )
                    self._spawn(self._reinitialise())
                continue
            if first == ord("!"):
                del buf[0]
                _LOGGER.debug("PCI reported buffer full / checksum error")
                continue
            if first in CONFIRMATION_CODES:
                if len(buf) < 2:
                    return
                status = buf[1]
                del buf[:2]
                fut = self._pending.get(first)
                if fut and not fut.done():
                    if status == CONFIRM_OK:
                        fut.set_result(True)
                    else:
                        _LOGGER.debug(
                            "Command %s not confirmed: %s",
                            chr(first),
                            CONFIRM_FAILURES.get(status, chr(status)),
                        )
                        fut.set_result(False)
                continue
            end = buf.find(b"\r")
            if end == -1:
                if len(buf) > 2048:
                    buf.clear()
                return
            line = bytes(buf[:end])
            del buf[: end + 1]
            self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        try:
            messages = decode_line(line)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to decode %s", line, exc_info=True)
            return
        if not messages:
            _LOGGER.debug("Ignored: %s", line)
        for msg in messages:
            if isinstance(msg, LightingEvent):
                _LOGGER.debug("Event: %s", msg)
                if self._on_lighting:
                    try:
                        self._on_lighting(msg)
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Error handling lighting event")
            elif isinstance(msg, StatusReport):
                _LOGGER.debug("Status: %s", msg)
                for collector in list(self._collectors):
                    collector.add(msg)
            elif isinstance(msg, ClockRequest) and self._answer_clock:
                self._spawn(self.send_time())
        if messages and self._on_packet:
            try:
                self._on_packet(messages)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error handling packet")

    async def _reinitialise(self) -> None:
        if not self._connected:
            return
        async with self._send_lock:
            await self._initialise()
        # Treat as a fresh connection so the owner resyncs.
        self._set_connected(False)
        self._set_connected(True)

    # ---------------------------------------------------------------- send
    def _write(self, data: bytes) -> None:
        if self._serial is None:
            raise CBusNotConnected("not connected")
        _LOGGER.debug("TX %s", data)
        self._serial.write(data)

    def _confirmation_code(self) -> int:
        code = CONFIRMATION_CODES[self._next_code]
        self._next_code = (self._next_code + 1) % len(CONFIRMATION_CODES)
        return code

    async def send_packet(self, packet: bytes) -> None:
        """Send a raw packet (with checksum) and wait for confirmation."""
        if not self._connected:
            raise CBusNotConnected("C-Bus interface not connected")
        async with self._send_lock:
            for attempt in range(1, self._command_retries + 1):
                if self._serial is None:
                    raise CBusNotConnected("C-Bus interface not connected")
                code = self._confirmation_code()
                fut: asyncio.Future[bool] = self._loop.create_future()
                self._pending[code] = fut
                try:
                    self._write(b"\\" + b16encode(packet) + bytes([code]) + b"\r")
                    ok = await asyncio.wait_for(fut, self._command_timeout)
                except TimeoutError:
                    ok = False
                finally:
                    self._pending.pop(code, None)
                if ok:
                    self._consecutive_failures = 0
                    return
                _LOGGER.debug(
                    "No confirmation for %s (attempt %d)", packet.hex(), attempt
                )
                await asyncio.sleep(0.25 * attempt)
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                _LOGGER.warning(
                    "C-Bus interface stopped confirming commands; reconnecting"
                )
                self._consecutive_failures = 0
                self.force_reconnect()
        raise CBusCommandFailed(f"C-Bus did not confirm command {packet.hex()}")

    # ---------------------------------------------------------- diagnostics
    async def open_raw(self) -> None:
        """Stop normal operation and open the port for raw diagnostics."""
        await self.stop()
        self._stopping = False
        await self._open()

    async def close_raw(self) -> None:
        self._close()

    def set_baudrate(self, baudrate: int) -> None:
        if self._serial is not None and hasattr(self._serial, "set_baudrate"):
            self._serial.set_baudrate(baudrate)

    def write_raw(self, data: bytes) -> None:
        """Write raw bytes to the port (diagnostics)."""
        self._write(data)

    def set_lines(self, dtr: bool, rts: bool) -> None:
        """Set handshake lines (diagnostics)."""
        if self._serial is not None and hasattr(self._serial, "set_lines"):
            self._serial.set_lines(dtr, rts)

    # ------------------------------------------------------------ lighting
    async def lighting_on(self, group: int, application: int = APP_LIGHTING):
        await self.send_packet(
            encode_point_to_multipoint(application, lighting_sal(CMD_ON, group))
        )

    async def lighting_off(self, group: int, application: int = APP_LIGHTING):
        await self.send_packet(
            encode_point_to_multipoint(application, lighting_sal(CMD_OFF, group))
        )

    async def lighting_ramp(
        self,
        group: int,
        level: int,
        seconds: float = 0,
        application: int = APP_LIGHTING,
    ) -> int:
        """Ramp a group to a level. Returns the actual ramp duration used."""
        code = duration_to_ramp_code(seconds)
        await self.send_packet(
            encode_point_to_multipoint(application, lighting_sal(code, group, level))
        )
        return RAMP_CODE_TO_SECONDS[code]

    async def lighting_terminate_ramp(
        self, group: int, application: int = APP_LIGHTING
    ):
        await self.send_packet(
            encode_point_to_multipoint(
                application, lighting_sal(CMD_TERMINATE_RAMP, group)
            )
        )

    # ------------------------------------------------------------- triggers
    async def trigger(self, group: int, action_selector: int) -> None:
        """Send a Trigger Control (scene) command."""
        await self.send_packet(
            encode_point_to_multipoint(APP_TRIGGER, trigger_sal(group, action_selector))
        )

    # -------------------------------------------------------------- status
    async def _status_request(
        self, application: int, start: int, count: int, level: bool
    ) -> dict[int, Any]:
        collector = _Collector("level" if level else "binary", application, start, count)
        self._collectors.append(collector)
        try:
            await self.send_packet(
                encode_point_to_multipoint(
                    APP_STATUS_REQUEST, status_request_sal(application, start, level)
                )
            )
            try:
                await asyncio.wait_for(collector.done.wait(), self._status_timeout)
            except TimeoutError:
                pass
            return dict(collector.values)
        finally:
            self._collectors.remove(collector)

    async def binary_status(self, application: int = APP_LIGHTING) -> dict[int, int]:
        """On/off/missing state of every group address of an application."""
        return await self._status_request(application, 0, 256, level=False)

    async def level_status(
        self, block: int, application: int = APP_LIGHTING
    ) -> dict[int, int | None]:
        """Levels of the 32 group addresses starting at ``block``."""
        block &= 0xE0
        return await self._status_request(application, block, 32, level=True)

    # --------------------------------------------------------------- clock
    async def send_time(self, when: datetime | None = None) -> None:
        when = when or datetime.now()
        try:
            await self.send_packet(
                encode_point_to_multipoint(APP_CLOCK, clock_sals(when))
            )
        except CBusError as err:
            _LOGGER.debug("Failed to send time: %s", err)

    async def _timesync(self) -> None:
        while True:
            await asyncio.sleep(self._timesync_interval)
            if self._connected:
                await self.send_time()
