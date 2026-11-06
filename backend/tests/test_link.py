import math
from dataclasses import replace

import pytest

from glucose_guardian.link import ACK_WINDOW, Link
from glucose_guardian.protocol import Reject, Report, ResetCause, crc16, encode_report

SESSION = 0x7F3A
BASE = Report(boot=0x00A1, seq=1, ack_session=SESSION, ack_seq=0, cause=ResetCause.EXTERNAL,
              button_stuck=False, short_presses=0, long_presses=0)


def line(report: Report) -> bytes:
    return encode_report(report)[:-1]


def connected(at_s: float) -> tuple[Link, int]:
    link = Link(session=SESSION)
    link.connection_opened()
    seq = link.next_seq(at_s)
    assert link.accept(line(replace(BASE, ack_seq=seq))).unexpected_reset is False
    return link, seq


def test_initial_connection_sets_liveness_from_acked_send_time():
    link = Link(session=SESSION)
    link.connection_opened()
    assert link.age_s(0) == math.inf
    seq = link.next_seq(10.0)
    events = link.accept(line(replace(BASE, ack_seq=seq, short_presses=4, long_presses=2)))
    assert (events.short_presses, events.long_presses, events.unexpected_reset) == (0, 0, False)
    assert link.age_s(12.5) == 2.5
    assert link.counts["connections"] == 1


@pytest.mark.parametrize("cause,unexpected", [
    (ResetCause.POWER_ON, False), (ResetCause.EXTERNAL, False), (ResetCause.UNKNOWN, False),
    (ResetCause.WATCHDOG, True), (ResetCause.BROWN_OUT, True),
])
def test_initial_connection_flags_only_abnormal_reset_causes(cause, unexpected):
    link = Link(session=SESSION)
    link.connection_opened()
    seq = link.next_seq(0)
    assert link.accept(line(replace(BASE, ack_seq=seq, cause=cause))).unexpected_reset is unexpected


def test_new_boot_while_connected_is_an_unexpected_reset_and_recovers():
    link, _ = connected(0)
    seq = link.next_seq(1)
    events = link.accept(line(replace(BASE, boot=0x00A2, seq=1, ack_seq=seq, short_presses=1)))
    assert events.unexpected_reset and events.short_presses == 1
    assert link.counts["unexpected_resets"] == 1
    seq = link.next_seq(2)
    events = link.accept(line(replace(BASE, boot=0x00A2, seq=2, ack_seq=seq)))
    assert events.unexpected_reset is False
    assert link.age_s(2) == 0


def test_reopened_port_is_an_initial_connection_not_a_reset():
    link, _ = connected(0)
    link.connection_opened()
    seq = link.next_seq(5)
    events = link.accept(line(replace(BASE, boot=0x00B0, seq=1, ack_seq=seq)))
    assert events.unexpected_reset is False
    assert link.counts["connections"] == 2


def corrupt(report_line: bytes) -> bytes:
    return report_line.replace(b" E ", b" P ")


def version_two(report_line: bytes) -> bytes:
    body = b"GG2" + report_line[3:].rpartition(b"*")[0]
    return body + b"*%04X" % crc16(body)


@pytest.mark.parametrize("mutate,reason", [
    (corrupt, "rejected_checksum"),
    (version_two, "rejected_version"),
    (lambda raw: raw + b"X", "rejected_no_checksum"),
])
def test_invalid_frames_never_refresh_liveness(mutate, reason):
    link, _ = connected(0)
    seq = link.next_seq(100)
    assert link.accept(mutate(line(replace(BASE, seq=2, ack_seq=seq)))).short_presses == 0
    assert link.age_s(100) == 100
    assert link.counts[reason] == 1


def test_oversize_marker_is_counted_and_ignored():
    link, _ = connected(0)
    link.accept(Reject.TOO_LONG)
    assert link.counts["rejected_too_long"] == 1 and link.age_s(5) == 5


@pytest.mark.parametrize("changes", [
    {"ack_session": 0x1111},
    {"ack_seq": 0x1234},
])
def test_unknown_or_foreign_acks_are_outdated(changes):
    link, _ = connected(0)
    link.next_seq(50)
    assert link.accept(line(replace(BASE, seq=2, **changes))).short_presses == 0
    assert link.age_s(50) == 50
    assert link.counts["rejected_outdated"] == 1


def test_acks_older_than_the_window_are_outdated():
    link = Link(session=SESSION)
    link.connection_opened()
    first = link.next_seq(0)
    for t in range(1, ACK_WINDOW + 1):
        link.next_seq(t)
    link.accept(line(replace(BASE, ack_seq=first)))
    assert link.counts["rejected_outdated"] == 1 and link.age_s(20) == math.inf


def test_duplicate_and_replayed_reports_are_rejected():
    link, seq = connected(0)
    later = link.next_seq(1)
    fresh = line(replace(BASE, seq=5, ack_seq=later, short_presses=2))
    assert link.accept(fresh).short_presses == 2
    assert link.accept(fresh).short_presses == 0
    replayed = line(replace(BASE, seq=4, ack_seq=later, short_presses=3))
    assert link.accept(replayed).short_presses == 0
    assert link.counts["rejected_duplicate"] == 2


def test_uno_sequence_wraps():
    link = Link(session=SESSION)
    link.connection_opened()
    for useq in (0xFFFE, 0xFFFF, 0x0000, 0x0001):
        seq = link.next_seq(useq)
        link.accept(line(replace(BASE, seq=useq, ack_seq=seq)))
    assert link.counts["accepted"] == 4 and link.counts["rejected_duplicate"] == 0


def test_pi_sequence_wraps():
    link = Link(session=SESSION)
    sequences = [link.next_seq(0) for _ in range(0x10002)]
    assert sequences[0xFFFF:] == [0xFFFF, 0x0000, 0x0001]


def test_button_counter_deltas_wrap():
    link, _ = connected(0)
    events = []
    for useq, shorts, longs in ((2, 0xFE, 0x10), (3, 0x01, 0x12)):
        seq = link.next_seq(useq)
        events.append(link.accept(line(replace(BASE, seq=useq, ack_seq=seq, short_presses=shorts,
                                                long_presses=longs))))
    assert [(e.short_presses, e.long_presses) for e in events] == [(0xFE, 0x10), (3, 2)]


def test_button_stuck_flag_follows_reports_and_clears_on_reopen():
    link, _ = connected(0)
    seq = link.next_seq(1)
    link.accept(line(replace(BASE, seq=2, ack_seq=seq, button_stuck=True)))
    assert link.button_stuck
    link.connection_opened()
    assert not link.button_stuck


def test_late_ack_of_older_frame_does_not_reduce_liveness():
    link = Link(session=SESSION)
    link.connection_opened()
    old = link.next_seq(0)
    new = link.next_seq(1)
    link.accept(line(replace(BASE, seq=1, ack_seq=new)))
    link.accept(line(replace(BASE, seq=2, ack_seq=old)))
    assert link.age_s(3) == 2
