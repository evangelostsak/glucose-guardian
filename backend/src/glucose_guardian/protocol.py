"""Serial protocol v1 framing: pure encoding, decoding and validation. No I/O."""
import re
from dataclasses import dataclass
from enum import Enum

from .alarms import GlucoseLevel, State

MAGIC = "GG1"
MAX_FRAME_BYTES = 64
MAX_LINE_BYTES = MAX_FRAME_BYTES - 1
SEQ_MODULUS = 0x10000
COUNTER_MODULUS = 0x100
GLUCOSE_CODES = {GlucoseLevel.LOW: "L", GlucoseLevel.URGENT_LOW: "U", GlucoseLevel.HIGH: "H"}
QUIET = "-"
FLAG_BUTTON_STUCK = 0x01

_STATE = re.compile(rb"GG1 S ([0-9A-F]{4}) ([0-9A-F]{4}) ([-LUH]) ([01])")
_REPORT = re.compile(rb"GG1 R ([0-9A-F]{4}) ([0-9A-F]{4}) ([0-9A-F]{4}) ([0-9A-F]{4}) "
                     rb"([PEBWU]) ([0-9A-F]{2}) ([0-9A-F]{2}) ([0-9A-F]{2})")
_CHECKSUM = re.compile(rb"[0-9A-F]{4}")


class Reject(str, Enum):
    TOO_LONG = "too_long"
    ENCODING = "encoding"
    NO_CHECKSUM = "no_checksum"
    CHECKSUM = "checksum"
    VERSION = "version"
    MALFORMED = "malformed"


class ResetCause(str, Enum):
    POWER_ON = "P"
    EXTERNAL = "E"
    BROWN_OUT = "B"
    WATCHDOG = "W"
    UNKNOWN = "U"


@dataclass(frozen=True)
class StateFrame:
    session: int
    seq: int
    glucose: str
    fault: bool


@dataclass(frozen=True)
class Report:
    boot: int
    seq: int
    ack_session: int
    ack_seq: int
    cause: ResetCause
    button_stuck: bool
    short_presses: int
    long_presses: int


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def seq_newer(candidate: int, reference: int) -> bool:
    delta = (candidate - reference) % SEQ_MODULUS
    return 0 < delta < SEQ_MODULUS // 2


def _frame(body: str) -> bytes:
    data = body.encode("ascii")
    return data + b"*%04X\n" % crc16(data)


def glucose_code(state: State) -> str:
    return GLUCOSE_CODES[state.glucose] if state.glucose_audible else QUIET


def encode_state(session: int, seq: int, state: State) -> bytes:
    return _frame(f"{MAGIC} S {session:04X} {seq:04X} {glucose_code(state)} "
                  f"{int(state.fault_audible)}")


def encode_report(report: Report) -> bytes:
    flags = FLAG_BUTTON_STUCK if report.button_stuck else 0
    return _frame(f"{MAGIC} R {report.boot:04X} {report.seq:04X} {report.ack_session:04X} "
                  f"{report.ack_seq:04X} {report.cause.value} {flags:02X} "
                  f"{report.short_presses:02X} {report.long_presses:02X}")


def _body(line: bytes) -> bytes | Reject:
    if len(line) > MAX_LINE_BYTES:
        return Reject.TOO_LONG
    if any(byte < 0x20 or byte > 0x7E for byte in line):
        return Reject.ENCODING
    body, star, checksum = line.rpartition(b"*")
    if not star or not _CHECKSUM.fullmatch(checksum):
        return Reject.NO_CHECKSUM
    if crc16(body) != int(checksum, 16):
        return Reject.CHECKSUM
    magic = body.split(b" ", 1)[0]
    if magic != MAGIC.encode():
        return Reject.VERSION if magic.startswith(b"GG") else Reject.MALFORMED
    return body


def decode_state(line: bytes) -> StateFrame | Reject:
    body = _body(line)
    if isinstance(body, Reject):
        return body
    match = _STATE.fullmatch(body)
    if match is None:
        return Reject.MALFORMED
    session, seq, glucose, fault = match.groups()
    return StateFrame(int(session, 16), int(seq, 16), glucose.decode(), fault == b"1")


def decode_report(line: bytes) -> Report | Reject:
    body = _body(line)
    if isinstance(body, Reject):
        return body
    match = _REPORT.fullmatch(body)
    if match is None:
        return Reject.MALFORMED
    boot, seq, ack_session, ack_seq, cause, flags, shorts, longs = match.groups()
    flag_bits = int(flags, 16)
    if flag_bits & ~FLAG_BUTTON_STUCK:
        return Reject.MALFORMED
    return Report(int(boot, 16), int(seq, 16), int(ack_session, 16), int(ack_seq, 16),
                  ResetCause(cause.decode()), bool(flag_bits & FLAG_BUTTON_STUCK),
                  int(shorts, 16), int(longs, 16))


class LineBuffer:
    def __init__(self, max_line_bytes: int) -> None:
        self._max = max_line_bytes
        self._partial = b""
        self._discarding = False

    def reset(self) -> None:
        self._partial = b""
        self._discarding = False

    def feed(self, data: bytes) -> list[bytes | Reject]:
        *complete, rest = data.split(b"\n")
        lines: list[bytes | Reject] = []
        for piece in complete:
            if self._discarding:
                self._discarding = False
            else:
                line = self._partial + piece
                lines.append(line if len(line) <= self._max else Reject.TOO_LONG)
            self._partial = b""
        if not self._discarding:
            self._partial += rest
            if len(self._partial) > self._max:
                lines.append(Reject.TOO_LONG)
                self._partial = b""
                self._discarding = True
        return lines
