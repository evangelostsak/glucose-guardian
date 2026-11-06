"""Linux serial transport for protocol v1; pyserial moves bytes only."""
import logging
from collections import Counter
from collections.abc import Callable

import serial

from .alarms import State
from .link import Link
from .protocol import MAX_LINE_BYTES, LineBuffer, encode_state
from .transport import Inbound

log = logging.getLogger(__name__)

BAUD_RATE = 115200
MAX_READ_BYTES = 1024

Opener = Callable[[str], serial.Serial]


def open_serial(device: str) -> serial.Serial:
    return serial.Serial(device, BAUD_RATE, timeout=0, write_timeout=0, exclusive=True)


class SerialTransport:
    def __init__(self, *, device: str, session: int, reconnect_initial_s: float,
                 reconnect_max_s: float, open_port: Opener) -> None:
        self.link = Link(session=session)
        self.counts: Counter[str] = Counter()
        self._device = device
        self._open_port = open_port
        self._initial_s = reconnect_initial_s
        self._max_s = reconnect_max_s
        self._backoff_s = reconnect_initial_s
        self._next_attempt_s: float | None = None
        self._buffer = LineBuffer(MAX_LINE_BYTES)
        self._port: serial.Serial | None = None

    @property
    def connected(self) -> bool:
        return self._port is not None

    def receive(self, monotonic_s: float) -> Inbound:
        short = long = 0
        unexpected_reset = False
        port = self._ensure_open(monotonic_s)
        if port is not None:
            try:
                data = port.read(MAX_READ_BYTES)
            except (serial.SerialException, OSError) as error:
                self._lost(monotonic_s, error)
                data = b""
            for item in self._buffer.feed(data):
                events = self.link.accept(item)
                short += events.short_presses
                long += events.long_presses
                unexpected_reset |= events.unexpected_reset
        return Inbound(short, long, self.link.age_s(monotonic_s), unexpected_reset,
                       self.link.button_stuck)

    def send(self, state: State, monotonic_s: float) -> None:
        port = self._port
        if port is None:
            return
        frame = encode_state(self.link.session, self.link.next_seq(monotonic_s), state)
        try:
            port.write(frame)
        except serial.SerialTimeoutException:
            self.counts["send_dropped"] += 1
        except (serial.SerialException, OSError) as error:
            self._lost(monotonic_s, error)

    def close(self) -> None:
        if self._port is not None:
            self._port.close()
            self._port = None

    def _ensure_open(self, monotonic_s: float) -> serial.Serial | None:
        if self._port is not None:
            return self._port
        if self._next_attempt_s is not None and monotonic_s < self._next_attempt_s:
            return None
        try:
            self._port = self._open_port(self._device)
        except (serial.SerialException, OSError) as error:
            self.counts["open_failures"] += 1
            self._schedule_retry(monotonic_s)
            log.warning("serial open failed: %s", type(error).__name__)
            return None
        self._backoff_s = self._initial_s
        self._next_attempt_s = None
        self._buffer.reset()
        self.link.connection_opened()
        self.counts["opened"] += 1
        log.info("serial port opened")
        return self._port

    def _lost(self, monotonic_s: float, error: Exception) -> None:
        self.counts["disconnects"] += 1
        log.warning("serial link lost: %s", type(error).__name__)
        try:
            self.close()
        except (serial.SerialException, OSError):
            self._port = None
        self._schedule_retry(monotonic_s)

    def _schedule_retry(self, monotonic_s: float) -> None:
        self._next_attempt_s = monotonic_s + self._backoff_s
        self._backoff_s = min(self._backoff_s * 2, self._max_s)
