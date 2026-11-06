import math
import os

import pytest
import serial
from harness import FakeUno, build_runtime, pump

from glucose_guardian.alarms import Fault, GlucoseLevel, State
from glucose_guardian.protocol import MAX_FRAME_BYTES, ResetCause, decode_state
from glucose_guardian.providers import SimulatedProvider
from glucose_guardian.serial_transport import MAX_READ_BYTES, SerialTransport, open_serial

SESSION = 0x7F3A
QUIET = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
URGENT = State(GlucoseLevel.URGENT_LOW, True, frozenset(), False, False)
K, R, S = Fault.COMMUNICATION, Fault.CONTROLLER_RESET, Fault.BUTTON_STUCK


class FakePort:
    def __init__(self, uno: FakeUno | None) -> None:
        self.uno = uno
        self.pending = b""
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.closed = False
        self.sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        data, self.pending = self.pending[:size], self.pending[size:]
        return data

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        if self.uno is not None:
            self.pending += self.uno.respond(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class Opener:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.attempts: list[float] = []
        self.ports: list[FakePort] = []

    def __call__(self, device: str) -> FakePort:
        self.attempts.append(self.clock[0])
        if not self.ports:
            raise serial.SerialException("no device")
        return self.ports.pop(0)


def fake_transport(opener: Opener) -> SerialTransport:
    return SerialTransport(device="/dev/null-uno", session=SESSION, reconnect_initial_s=1,
                           reconnect_max_s=4, open_port=opener)


@pytest.fixture
def pty():
    master, slave = os.openpty()
    os.set_blocking(master, False)
    fds = {"master": master, "slave": slave}
    yield master, os.ttyname(slave), fds
    for fd in fds.values():
        if fd is not None:
            os.close(fd)


def test_reconnect_backoff_doubles_caps_and_resets():
    clock = [0.0]
    opener = Opener(clock)
    transport = fake_transport(opener)
    for clock[0] in (0, 0.5, 1, 2, 3, 6, 7, 10):
        assert transport.receive(clock[0]).link_age_s == math.inf
    assert opener.attempts == [0, 1, 3, 7]
    opener.ports.append(FakePort(FakeUno(1)))
    clock[0] = 11
    transport.receive(11)
    assert transport.connected and opener.attempts[-1] == 11
    transport._port.read_error = serial.SerialException("unplugged")
    transport.receive(12)
    assert not transport.connected and transport.counts["disconnects"] == 1
    for clock[0] in (12.5, 13):
        transport.receive(clock[0])
    assert opener.attempts[-1] == 13


def test_round_trip_over_a_fake_port_and_bounded_reads():
    opener = Opener([0.0])
    port = FakePort(FakeUno(1))
    opener.ports.append(port)
    transport = fake_transport(opener)
    transport.receive(0)
    transport.send(QUIET, 0)
    inbound = transport.receive(1)
    assert inbound.link_age_s == 1 and not inbound.unexpected_reset
    assert set(port.sizes) == {MAX_READ_BYTES}


def test_write_timeout_drops_the_frame_but_keeps_the_port():
    opener = Opener([0.0])
    port = FakePort(None)
    port.write_error = serial.SerialTimeoutException("full")
    opener.ports.append(port)
    transport = fake_transport(opener)
    transport.receive(0)
    transport.send(QUIET, 0)
    assert transport.connected and transport.counts["send_dropped"] == 1


def test_write_error_disconnects_and_send_while_closed_is_a_no_op():
    opener = Opener([0.0])
    port = FakePort(None)
    port.write_error = serial.SerialException("gone")
    opener.ports.append(port)
    transport = fake_transport(opener)
    transport.receive(0)
    transport.send(QUIET, 0)
    assert not transport.connected and port.closed
    transport.send(QUIET, 0.5)
    assert transport.counts["disconnects"] == 1


def test_reconnect_with_a_new_boot_is_an_initial_connection():
    clock = [0.0]
    opener = Opener(clock)
    first = FakePort(FakeUno(1))
    opener.ports += [first, FakePort(FakeUno(2))]
    transport = fake_transport(opener)
    transport.receive(0)
    transport.send(QUIET, 0)
    assert transport.receive(1).link_age_s == 1
    first.read_error = serial.SerialException("unplugged")
    transport.receive(2)
    transport.receive(3)
    transport.send(QUIET, 3)
    inbound = transport.receive(4)
    assert inbound.link_age_s == 1 and not inbound.unexpected_reset
    assert transport.link.counts["connections"] == 2


def test_real_pty_round_trip_and_frame_contents(pty):
    master, path, _ = pty
    transport = SerialTransport(device=path, session=SESSION, reconnect_initial_s=1,
                                reconnect_max_s=4, open_port=open_serial)
    uno = FakeUno(0x00A1)
    try:
        transport.receive(0)
        transport.send(URGENT, 0)
        pump(master, uno)
        assert [(f.session, f.seq, f.glucose, f.fault) for f in uno.states] == [
            (SESSION, 0, "U", False)]
        inbound = transport.receive(0.25)
        assert inbound.link_age_s == 0.25 and not inbound.unexpected_reset
    finally:
        transport.close()


def test_real_pty_flood_is_bounded_and_recovers(pty):
    master, path, _ = pty
    transport = SerialTransport(device=path, session=SESSION, reconnect_initial_s=1,
                                reconnect_max_s=4, open_port=open_serial)
    uno = FakeUno(0x00A1)
    try:
        transport.receive(0)
        os.write(master, b"\xff" * 3000)
        transport.send(QUIET, 0)
        pump(master, uno)
        ages = [transport.receive(t).link_age_s for t in (1, 2, 3, 4)]
        assert ages == [math.inf] * 4
        transport.send(QUIET, 5)
        pump(master, uno)
        assert transport.receive(6).link_age_s == 1
        assert transport.link.counts["rejected_too_long"] == 1
        assert transport.link.counts["accepted"] == 1
    finally:
        transport.close()


def test_real_pty_hangup_is_detected(pty):
    master, path, fds = pty
    transport = SerialTransport(device=path, session=SESSION, reconnect_initial_s=1,
                                reconnect_max_s=4, open_port=open_serial)
    transport.receive(0)
    assert transport.connected
    os.close(master)
    fds["master"] = None
    transport.receive(1)
    assert not transport.connected and transport.counts["disconnects"] == 1


def test_sent_frames_respect_the_size_bound(pty):
    master, path, _ = pty
    transport = SerialTransport(device=path, session=0xFFFF, reconnect_initial_s=1,
                                reconnect_max_s=4, open_port=open_serial)
    try:
        transport.receive(0)
        transport.send(URGENT, 0)
        frame = os.read(master, 4096)
        assert len(frame) <= MAX_FRAME_BYTES and frame.endswith(b"\n")
        assert decode_state(frame[:-1]).session == 0xFFFF
    finally:
        transport.close()


class PtyRig:
    def __init__(self, master: int, path: str) -> None:
        self.master = master
        self.clock = [0.0]
        self.uno = FakeUno(0x00A1)
        self.transport = SerialTransport(device=path, session=SESSION, reconnect_initial_s=1,
                                         reconnect_max_s=4, open_port=open_serial)
        self.runtime = build_runtime(transport=self.transport, monotonic=lambda: self.clock[0],
                                     provider=SimulatedProvider((50,)))

    def run(self, seconds: int) -> State:
        for _ in range(seconds):
            state = self.runtime.tick()
            pump(self.master, self.uno)
            self.clock[0] += 1
        return state


@pytest.fixture
def rig(pty):
    master, path, _ = pty
    rig = PtyRig(master, path)
    yield rig
    rig.transport.close()


def test_runtime_over_pty_link_up_and_uno_silence_times_out(rig):
    assert K in rig.run(1).faults
    assert rig.run(1).faults == set()
    rig.uno.silent = True
    assert rig.run(5).faults == set()
    assert rig.run(1).faults == {K}
    rig.uno.silent = False
    assert rig.run(2).faults == set()


def test_runtime_over_pty_corrupted_replies_never_refresh_liveness(rig):
    rig.run(2)
    rig.uno.corrupt = True
    assert rig.run(6).faults == {K}
    assert rig.transport.link.counts["rejected_checksum"] >= 5


def test_runtime_over_pty_button_counters_map_to_channels(rig):
    rig.run(2)
    rig.uno.short_presses += 1
    state = rig.run(2)
    assert state.glucose_silenced and not state.faults_silenced
    rig.uno.button_stuck = True
    assert rig.run(2).fault_audible
    rig.uno.long_presses += 1
    state = rig.run(2)
    assert state.faults_silenced and state.faults == {S} and state.glucose_silenced


def test_runtime_over_pty_unexpected_reset_latches_until_long_press(rig):
    rig.run(3)
    rig.uno.reboot(0x00A2, ResetCause.WATCHDOG)
    state = rig.run(2)
    assert state.faults == {R} and state.fault_audible
    rig.uno.short_presses += 1
    assert R in rig.run(2).faults
    rig.uno.long_presses += 1
    assert rig.run(2).faults == set()


def test_runtime_over_pty_stuck_button(rig):
    rig.run(2)
    rig.uno.button_stuck = True
    assert rig.run(2).faults == {S}
    rig.uno.button_stuck = False
    assert rig.run(2).faults == set()
