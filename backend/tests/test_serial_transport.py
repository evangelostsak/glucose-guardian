import math
import os
import threading

import pytest
import serial
from harness import FakeUno, build_runtime, pump

from glucose_guardian.alarms import Fault, GlucoseLevel, State
from glucose_guardian.protocol import MAX_FRAME_BYTES, ResetCause, decode_state, encode_state
from glucose_guardian.providers import SimulatedProvider
from glucose_guardian.serial_transport import MAX_READ_BYTES, SerialTransport, open_serial

SESSION = 0x7F3A
QUIET = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
URGENT = State(GlucoseLevel.URGENT_LOW, True, frozenset(), False, False)
K, R, S = Fault.COMMUNICATION, Fault.CONTROLLER_RESET, Fault.BUTTON_STUCK
FRAME_BYTES = len(encode_state(SESSION, 0, QUIET))


class FakePort:
    def __init__(self, uno: FakeUno | None) -> None:
        self.uno = uno
        self.pending = b""
        self.read_error: Exception | None = None
        self.write_results: list[int | Exception] = []
        self.discard_error: Exception | None = None
        self.calls: list[str] = []
        self.closed = False
        self.sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        data, self.pending = self.pending[:size], self.pending[size:]
        return data

    def write(self, data: bytes) -> int:
        self.calls.append("write")
        result = self.write_results.pop(0) if self.write_results else len(data)
        if isinstance(result, Exception):
            raise result
        if self.uno is not None:
            self.pending += self.uno.respond(data[:result])
        return result

    def discard_output(self) -> None:
        self.calls.append("discard")
        if self.discard_error is not None:
            raise self.discard_error

    def close(self) -> None:
        self.calls.append("close")
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
    for clock[0] in (13, 15.5):
        transport.receive(clock[0])
    assert opener.attempts[-1] == 11
    opener.ports.append(FakePort(FakeUno(1)))
    clock[0] = 16
    transport.receive(16)
    assert transport.connected and opener.attempts[-1] == 16


def failing_port(mode: str) -> FakePort:
    port = FakePort(FakeUno(1))
    if mode == "write":
        port.write_results = [0]
    else:
        port.read_error = serial.SerialException("gone")
    return port


@pytest.mark.parametrize("mode", ["write", "read"])
def test_backoff_survives_repeated_short_lived_connections(mode):
    clock = [0.0]
    opener = Opener(clock)
    opener.ports += [failing_port(mode) for _ in range(6)]
    transport = fake_transport(opener)
    for step in range(33):
        clock[0] = step / 2
        transport.receive(clock[0])
        transport.send(QUIET, clock[0])
    assert opener.attempts == [0, 1, 3, 7, 11, 15]
    assert transport.counts["opened"] == 6 and transport.counts["disconnects"] == 6
    assert not transport.connected


def test_backoff_resets_only_after_a_stable_connection():
    clock = [0.0]
    opener = Opener(clock)
    stable = FakePort(FakeUno(1))
    opener.ports += [failing_port("write") for _ in range(3)] + [stable]
    opener.ports += [failing_port("write") for _ in range(2)]
    transport = fake_transport(opener)
    for clock[0] in (0, 1, 3):
        transport.receive(clock[0])
        transport.send(QUIET, clock[0])
    clock[0] = 7
    transport.receive(7)
    for t in (7, 8, 9, 10):
        transport.send(QUIET, t)
    assert transport.connected
    stable.write_results = [0]
    transport.send(QUIET, 11)
    for step in range(22, 40):
        clock[0] = step / 2
        transport.receive(clock[0])
        transport.send(QUIET, clock[0])
    assert opener.attempts == [0, 1, 3, 7, 12, 14, 18]


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


def connected_transport(port: FakePort) -> tuple[SerialTransport, Opener]:
    opener = Opener([0.0])
    opener.ports.append(port)
    transport = fake_transport(opener)
    transport.receive(0)
    assert transport.connected
    return transport, opener


def test_complete_write_is_recorded_as_sent():
    transport, _ = connected_transport(FakePort(FakeUno(1)))
    transport.send(QUIET, 0)
    assert transport.connected and transport.counts["frames_sent"] == 1
    assert transport.receive(0.5).link_age_s == 0.5


@pytest.mark.parametrize("written", [0, 1, 10, FRAME_BYTES - 1])
def test_incomplete_write_discards_output_and_reconnects(written):
    port = FakePort(FakeUno(1))
    port.write_results = [written]
    transport, opener = connected_transport(port)
    transport.send(QUIET, 0)
    assert port.calls == ["write", "discard", "close"]
    assert not transport.connected
    assert (transport.counts["incomplete_writes"], transport.counts["frames_sent"]) == (1, 0)
    assert not transport.link._sent
    assert transport.receive(0.5).link_age_s == math.inf
    assert opener.attempts == [0.0]


@pytest.mark.parametrize("error", [
    serial.SerialTimeoutException("full"),
    serial.SerialException("gone"),
    OSError("io"),
])
def test_write_exception_discards_output_and_reconnects(error):
    port = FakePort(None)
    port.write_results = [error]
    transport, _ = connected_transport(port)
    transport.send(QUIET, 0)
    assert port.calls == ["write", "discard", "close"]
    assert (transport.counts["failed_writes"], transport.counts["disconnects"]) == (1, 1)
    transport.send(QUIET, 0.5)
    assert port.calls == ["write", "discard", "close"]


def test_recovery_still_closes_when_discard_fails():
    port = FakePort(None)
    port.write_results = [3]
    port.discard_error = serial.SerialException("gone")
    transport, _ = connected_transport(port)
    transport.send(QUIET, 0)
    assert port.closed and not transport.connected
    assert transport.counts["discard_failures"] == 1


def test_truncated_frame_never_refreshes_liveness_and_recovers():
    clock = [0.0]
    opener = Opener(clock)
    uno = FakeUno(1)
    first, second = FakePort(uno), FakePort(uno)
    first.write_results = [FRAME_BYTES - 1]
    opener.ports += [first, second]
    transport = fake_transport(opener)
    transport.receive(0)
    transport.send(QUIET, 0)
    assert not transport.connected
    for clock[0] in (0.5, 1):
        transport.receive(clock[0])
    assert transport.connected
    transport.send(QUIET, 1)
    assert uno.states == []
    assert transport.receive(2).link_age_s == math.inf
    transport.send(QUIET, 2)
    assert len(uno.states) == 1
    assert transport.receive(3).link_age_s == 1
    assert transport.counts["incomplete_writes"] == 1


def test_send_while_closed_is_a_no_op():
    transport = fake_transport(Opener([0.0]))
    transport.send(QUIET, 0)
    assert transport.counts == {}


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


def test_real_pty_full_buffer_never_blocks_and_recovers(pty):
    master, path, _ = pty
    transport = SerialTransport(device=path, session=SESSION, reconnect_initial_s=1,
                                reconnect_max_s=4, open_port=open_serial)
    transport.receive(0)

    def flood() -> None:
        for _ in range(100_000):
            transport.send(QUIET, 0)
            if not transport.connected:
                return

    worker = threading.Thread(target=flood, daemon=True)
    worker.start()
    worker.join(5)
    try:
        assert not worker.is_alive()
        assert not transport.connected
        assert transport.counts["incomplete_writes"] == 1
        assert transport.counts["frames_sent"] > 10
        while True:
            try:
                os.read(master, 65536)
            except BlockingIOError:
                break
        transport.receive(1)
        assert transport.connected
        transport.send(URGENT, 1)
        frame = os.read(master, 4096)
        assert decode_state(frame[:-1]).glucose == "U"
    finally:
        transport.close()


def test_pyserial_port_full_buffer_returns_zero_without_blocking(pty):
    _, path, _ = pty
    port = open_serial(path)
    frame = encode_state(SESSION, 0, QUIET)
    complete = 0
    while complete < 10_000 and port.write(frame) == len(frame):
        complete += 1
    assert 10 < complete < 10_000
    results: list[int] = []
    worker = threading.Thread(target=lambda: results.append(port.write(frame)), daemon=True)
    worker.start()
    worker.join(2)
    blocked = worker.is_alive()
    if not blocked:
        port.discard_output()
        port.close()
    assert not blocked and results == [0]
