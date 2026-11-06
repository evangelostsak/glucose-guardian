import pytest

from glucose_guardian.alarms import Fault, GlucoseLevel, State
from glucose_guardian.protocol import (
    MAX_FRAME_BYTES,
    MAX_LINE_BYTES,
    LineBuffer,
    Reject,
    Report,
    ResetCause,
    StateFrame,
    crc16,
    decode_report,
    decode_state,
    encode_report,
    encode_state,
    seq_newer,
)

URGENT = State(GlucoseLevel.URGENT_LOW, True, frozenset({Fault.COMMUNICATION}), False, False)
QUIET = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
STATE_VECTOR = b"GG1 S 7F3A 0001 U 1*03EE\n"
QUIET_VECTOR = b"GG1 S 7F3A FFFF - 0*C5CA\n"
REPORT = Report(0x00A1, 0x0002, 0x7F3A, 0x0001, ResetCause.EXTERNAL, False, 3, 1)
REPORT_VECTOR = b"GG1 R 00A1 0002 7F3A 0001 E 00 03 01*505C\n"
MAX_REPORT = Report(0xFFFF, 0xFFFF, 0x7F3A, 0xFFFF, ResetCause.WATCHDOG, True, 0xFF, 0x00)
MAX_REPORT_VECTOR = b"GG1 R FFFF FFFF 7F3A FFFF W 01 FF 00*047D\n"


def signed(body: bytes) -> bytes:
    return body + b"*%04X" % crc16(body)


def test_crc16_ccitt_false_check_value():
    assert crc16(b"123456789") == 0x29B1


def test_state_frame_vectors():
    assert encode_state(0x7F3A, 0x0001, URGENT) == STATE_VECTOR
    assert encode_state(0x7F3A, 0xFFFF, QUIET) == QUIET_VECTOR
    assert decode_state(STATE_VECTOR[:-1]) == StateFrame(0x7F3A, 0x0001, "U", True)


@pytest.mark.parametrize("state,code", [
    (State(GlucoseLevel.LOW, True, frozenset(), False, False), b" L 0"),
    (State(GlucoseLevel.HIGH, True, frozenset(), False, False), b" H 0"),
    (State(GlucoseLevel.URGENT_LOW, False, frozenset(), True, False), b" - 0"),
    (State(GlucoseLevel.UNKNOWN, False, frozenset({Fault.CLOCK}), False, True), b" - 0"),
    (State(GlucoseLevel.UNKNOWN, False, frozenset({Fault.CLOCK}), False, False), b" - 1"),
])
def test_state_frame_carries_only_audible_alarms(state, code):
    assert code + b"*" in encode_state(1, 2, state)


def test_report_vectors_round_trip():
    assert encode_report(REPORT) == REPORT_VECTOR
    assert encode_report(MAX_REPORT) == MAX_REPORT_VECTOR
    assert decode_report(REPORT_VECTOR[:-1]) == REPORT
    assert decode_report(MAX_REPORT_VECTOR[:-1]) == MAX_REPORT


def test_frames_fit_the_bound():
    assert len(MAX_REPORT_VECTOR) <= MAX_FRAME_BYTES
    assert len(encode_state(0xFFFF, 0xFFFF, URGENT)) <= MAX_FRAME_BYTES


@pytest.mark.parametrize("line,reason", [
    (b"G" * (MAX_LINE_BYTES + 1), Reject.TOO_LONG),
    (REPORT_VECTOR[:-1] + b"\r", Reject.ENCODING),
    (b"\x00" + REPORT_VECTOR[:-1], Reject.ENCODING),
    (b"GG1 R 00A1 0002 7F3A 0001 E 00 03 01", Reject.NO_CHECKSUM),
    (REPORT_VECTOR[:-5] + b"505c", Reject.NO_CHECKSUM),
    (REPORT_VECTOR[:-5] + b"505D", Reject.CHECKSUM),
    (REPORT_VECTOR.replace(b"00A1", b"00A2")[:-1], Reject.CHECKSUM),
    (signed(b"GG2 R 00A1 0002 7F3A 0001 E 00 03 01"), Reject.VERSION),
    (signed(b"HELLO"), Reject.MALFORMED),
    (signed(b"GG1"), Reject.MALFORMED),
    (signed(b"GG12 R 00A1 0002 7F3A 0001 E 00 03 01"), Reject.VERSION),
    (signed(b"GG1 R 00a1 0002 7F3A 0001 E 00 03 01"), Reject.MALFORMED),
    (signed(b"GG1 R 00A1 0002 7F3A 0001 X 00 03 01"), Reject.MALFORMED),
    (signed(b"GG1 R 00A1 0002 7F3A 0001 E 02 03 01"), Reject.MALFORMED),
    (signed(b"GG1 R 00A1 0002 7F3A 0001 E 00 03 01 00"), Reject.MALFORMED),
    (STATE_VECTOR[:-1], Reject.MALFORMED),
])
def test_report_rejections(line, reason):
    assert decode_report(line) is reason


@pytest.mark.parametrize("line,reason", [
    (b"GG2 S 7F3A 0001 U 1*32C8", Reject.VERSION),
    (b"GG1 S 7f3a 0001 U 1*5443", Reject.MALFORMED),
    (b"GG1 S 7F3A 0001 X 1*41BF", Reject.MALFORMED),
    (REPORT_VECTOR[:-1], Reject.MALFORMED),
])
def test_state_rejections(line, reason):
    assert decode_state(line) is reason


@pytest.mark.parametrize("candidate,reference,newer", [
    (1, 0, True), (0, 0, False), (0, 1, False), (0, 0xFFFF, True), (5, 0xFFFE, True),
    (0x7FFF, 0, True), (0x8000, 0, False), (0xFFFF, 0, False),
])
def test_sequence_comparison_wraps(candidate, reference, newer):
    assert seq_newer(candidate, reference) is newer


def test_line_buffer_reassembles_partial_lines():
    buffer = LineBuffer(MAX_LINE_BYTES)
    assert buffer.feed(REPORT_VECTOR[:10]) == []
    assert buffer.feed(REPORT_VECTOR[10:] + REPORT_VECTOR) == [REPORT_VECTOR[:-1]] * 2


def test_line_buffer_length_boundary():
    buffer = LineBuffer(MAX_LINE_BYTES)
    assert buffer.feed(b"A" * MAX_LINE_BYTES + b"\n") == [b"A" * MAX_LINE_BYTES]
    assert buffer.feed(b"A" * (MAX_LINE_BYTES + 1) + b"\n") == [Reject.TOO_LONG]


def test_line_buffer_flood_is_bounded_and_recovers():
    buffer = LineBuffer(MAX_LINE_BYTES)
    results = [item for _ in range(100) for item in buffer.feed(b"x" * 1000)]
    assert results == [Reject.TOO_LONG]
    assert len(buffer._partial) <= MAX_LINE_BYTES
    assert buffer.feed(b"tail\n" + REPORT_VECTOR) == [REPORT_VECTOR[:-1]]


def test_line_buffer_reset_drops_partial_data():
    buffer = LineBuffer(MAX_LINE_BYTES)
    buffer.feed(b"GG1 R 00")
    buffer.reset()
    assert buffer.feed(REPORT_VECTOR) == [REPORT_VECTOR[:-1]]
