"""Real-time protocol exchanges with a controller. Every wait has a deadline."""
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

import serial

from ..alarms import GlucoseLevel, State
from ..link import Link, LinkEvents
from ..protocol import MAX_LINE_BYTES, LineBuffer, Reject, Report, decode_report, encode_state
from ..serial_transport import BAUD_RATE

BENCH_STATE = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
WATCHDOG_TEST_BANNER = b"GGTEST WATCHDOG-HANG FIRMWARE NOT FOR USE"
READ_TIMEOUT_S = 0.02
WRITE_TIMEOUT_S = 1.0
MAX_READ_BYTES = 1024
SENT_HISTORY = 64


class BenchError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    seq: int
    sent_at: float
    received_at: float
    report: Report
    accepted: bool
    events: LinkEvents

    @property
    def latency_s(self) -> float:
        return self.received_at - self.sent_at


def open_bench_port(device: str) -> serial.Serial:
    return serial.Serial(device, BAUD_RATE, timeout=READ_TIMEOUT_S,
                         write_timeout=WRITE_TIMEOUT_S, exclusive=True)


class Probe:
    def __init__(self, port: serial.Serial, *, session: int,
                 clock: Callable[[], float]) -> None:
        self.port = port
        self.link = Link(session=session)
        self.link.connection_opened()
        self.rejects: Counter[str] = Counter()
        self.banner_seen = False
        self.other_lines: deque[bytes] = deque(maxlen=32)
        self._clock = clock
        self._buffer = LineBuffer(MAX_LINE_BYTES)
        self._sent: dict[int, float] = {}

    def send(self) -> int:
        seq = self.link.next_seq()
        frame = encode_state(self.link.session, seq, BENCH_STATE)
        self.send_raw(frame)
        now = self._clock()
        self.link.sent(seq, now)
        self._sent[seq] = now
        while len(self._sent) > SENT_HISTORY:
            del self._sent[next(iter(self._sent))]
        return seq

    def send_raw(self, data: bytes) -> None:
        written = self.port.write(data)
        if written != len(data):
            raise BenchError(f"incomplete write: {written} of {len(data)} bytes")

    def use_session(self, session: int) -> None:
        self.link.session = session

    def poll(self) -> list[Reply]:
        data = self.port.read(MAX_READ_BYTES)
        now = self._clock()
        replies = []
        for item in self._buffer.feed(data):
            if isinstance(item, bytes) and item.startswith(WATCHDOG_TEST_BANNER):
                self.banner_seen = True
                continue
            decoded = item if isinstance(item, Reject) else decode_report(item)
            if isinstance(decoded, Reject):
                self.rejects[decoded.value] += 1
                if isinstance(item, bytes):
                    self.other_lines.append(item[:MAX_LINE_BYTES])
                continue
            accepted_before = self.link.counts["accepted"]
            events = self.link.accept(item)
            sent_at = self._sent.get(decoded.ack_seq)
            if decoded.ack_session != self.link.session or sent_at is None:
                self.rejects["unmatched"] += 1
                continue
            replies.append(Reply(decoded.ack_seq, sent_at, now, decoded,
                                 self.link.counts["accepted"] > accepted_before, events))
        return replies

    def listen(self, duration_s: float) -> list[Reply]:
        deadline = self._clock() + duration_s
        replies: list[Reply] = []
        while self._clock() < deadline:
            replies += self.poll()
        return replies

    def exchange(self, timeout_s: float) -> Reply | None:
        seq = self.send()
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            for reply in self.poll():
                if reply.seq == seq:
                    return reply
        return None

    def first_reply(self, timeout_s: float, interval_s: float) -> Reply | None:
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            reply = self.exchange(min(interval_s, max(0.0, deadline - self._clock())))
            if reply is not None:
                return reply
        return None
