import threading
from dataclasses import asdict, replace

import pytest
from harness import NOW, POLICY, Harness, build_runtime

from glucose_guardian.alarms import Fault, GlucoseLevel, Policy
from glucose_guardian.display import HOLD_PROMPT, MemoryDisplay
from glucose_guardian.providers import Reading, SimulatedProvider
from glucose_guardian.simulation import SCENARIOS, Fetch, Scenario
from glucose_guardian.transport import Inbound, SimulatedTransport

U, L, N, H, X = (GlucoseLevel.URGENT_LOW, GlucoseLevel.LOW, GlucoseLevel.NORMAL,
                 GlucoseLevel.HIGH, GlucoseLevel.UNKNOWN)
A, M, V, C, K = (Fault.ACQUISITION, Fault.MISSING_DATA, Fault.INVALID_READING, Fault.CLOCK,
                 Fault.COMMUNICATION)
R, S = Fault.CONTROLLER_RESET, Fault.BUTTON_STUCK

CHECKPOINTS = {
    "normal": [(10, N, set())],
    "hypoglycemia": [(100, N, set()), (350, L, set()), (650, U, set()), (1000, N, set())],
    "hyperglycemia": [(100, N, set()), (350, H, set()), (1000, N, set())],
    "stale_data": [(100, U, set()), (600, U, set()), (1000, U, {M}), (1600, N, set())],
    "missing_data": [(10, X, {M}), (700, N, set())],
    "invalid_readings": [(100, N, set()), (350, X, {V}), (700, N, set())],
    "acquisition_timeout": [(305, N, set()), (350, N, {A}), (700, N, set())],
    "acquisition_errors": [(100, N, set()), (350, N, {A}), (700, N, set())],
    "clock_failure": [(10, X, {C}), (350, N, set())],
    "communication_loss": [(100, N, set()), (350, N, {K}), (700, N, set())],
    "simultaneous_faults": [(100, U, set()), (1000, U, {A, M, K}), (1600, N, set())],
}


def test_every_scenario_has_checkpoints():
    assert set(CHECKPOINTS) == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(CHECKPOINTS))
def test_scenario_transitions_and_recovery(name):
    harness = Harness(SCENARIOS[name])
    for t, glucose, faults in CHECKPOINTS[name]:
        state = harness.run_until(t)
        assert (state.glucose, state.faults) == (glucose, faults), t
        assert harness.transport.last_sent is state
        assert harness.display.last_frame.glucose is glucose
    assert harness.runtime.acquisition.attempts <= 1 + CHECKPOINTS[name][-1][0] // 60


def test_stale_urgent_low_is_displayed_as_last_known():
    harness = Harness(SCENARIOS["stale_data"])
    state = harness.run_until(1000)
    frame = harness.display.last_frame
    assert (frame.glucose, frame.mg_dl, frame.last_known, frame.data_age_s) == (U, 50, True, 700)
    assert frame.faults == (M,)
    assert state.glucose_audible and state.fault_audible
    current = harness.run_until(1600)
    assert not harness.display.last_frame.last_known and current.glucose_current


def test_acknowledging_stale_glucose_does_not_silence_missing_data():
    harness = Harness(SCENARIOS["stale_data"])
    harness.run_until(1000)
    harness.runtime.machine.acknowledge_glucose(harness.t)
    state = harness.run_until(1001)
    assert state.glucose_silenced and M in state.faults and state.fault_audible


def test_short_press_silences_glucose_only_and_prompts_for_faults():
    harness = Harness(SCENARIOS["stale_data"])
    assert harness.display.last_frame is None
    assert harness.run_until(1000).glucose_audible
    assert harness.display.last_frame.prompt is None
    harness.transport.press()
    state = harness.run_until(1001)
    assert state.glucose_silenced and state.fault_audible and M in state.faults
    frame = harness.display.last_frame
    assert (frame.glucose, frame.faults, frame.prompt) == (U, (M,), HOLD_PROMPT)


def test_long_press_silences_faults_only_and_keeps_warnings_visible():
    harness = Harness(SCENARIOS["stale_data"])
    harness.run_until(1000)
    harness.transport.hold()
    state = harness.run_until(1001)
    assert state.faults_silenced and state.glucose_audible
    frame = harness.display.last_frame
    assert (frame.glucose, frame.last_known, frame.faults, frame.prompt) == (U, True, (M,), None)


def test_repeated_presses_do_not_extend_silence():
    harness = Harness(SCENARIOS["stale_data"])
    harness.run_until(1000)
    harness.transport.press()
    harness.transport.hold()
    harness.run_until(1030)
    harness.transport.press()
    harness.transport.hold()
    assert harness.run_until(1060).faults_silenced
    state = harness.run_until(1061)
    assert state.glucose_audible and state.fault_audible


def test_glucose_and_fault_silence_durations_are_independent():
    clock = [0.0]
    policy = Policy(**{**asdict(POLICY), "glucose_acknowledgement_s": 60,
                       "fault_acknowledgement_s": 30})
    transport = SimulatedTransport(heartbeat_ok=lambda t: t < 10)
    runtime = build_runtime(policy=policy, transport=transport,
                            provider=SimulatedProvider((50,)), monotonic=lambda: clock[0])
    for clock[0] in range(20):
        runtime.tick()
    clock[0] = 20
    transport.press()
    transport.hold()
    assert runtime.tick().faults_silenced
    clock[0] = 49
    state = runtime.tick()
    assert state.faults_silenced and state.glucose_silenced
    clock[0] = 50
    state = runtime.tick()
    assert state.fault_audible and state.glucose_silenced
    clock[0] = 80
    assert runtime.tick().glucose_audible


def test_unexpected_reset_latches_until_long_press():
    harness = Harness(SCENARIOS["normal"])
    harness.run_until(10)
    harness.transport.reset_pending = True
    state = harness.run_until(11)
    assert state.faults == {R} and state.fault_audible
    harness.transport.press()
    assert harness.run_until(100).faults == {R}
    harness.transport.hold()
    assert harness.run_until(101).faults == set()


def test_reset_during_fault_silence_sounds():
    harness = Harness(SCENARIOS["communication_loss"])
    harness.run_until(350)
    harness.transport.hold()
    assert harness.run_until(351).faults_silenced
    harness.transport.reset_pending = True
    state = harness.run_until(352)
    assert state.faults == {K, R} and state.fault_audible


def test_stuck_button_fault_follows_the_controller_flag():
    harness = Harness(SCENARIOS["normal"])
    harness.run_until(10)
    harness.transport.button_stuck = True
    state = harness.run_until(11)
    assert state.faults == {S} and state.fault_audible
    harness.transport.button_stuck = False
    assert harness.run_until(12).faults == set()


def test_new_fault_during_acknowledgement_sounds():
    harness = Harness(SCENARIOS["simultaneous_faults"])
    assert harness.run_until(300).faults == {A}
    harness.transport.hold()
    assert harness.run_until(304).faults_silenced
    state = harness.run_until(305)
    assert state.faults == {A, K} and state.fault_audible


def test_startup_is_unknown_and_faulted_until_data_arrives():
    hung_start = Scenario("slow_start", (
        replace(SCENARIOS["normal"].segments[0], duration_s=30, fetch=Fetch.HANG),
        SCENARIOS["normal"].segments[0]))
    harness = Harness(hung_start)
    assert (harness.runtime.machine.state.glucose, harness.display.last_frame) == (X, None)
    state = harness.run_until(0)
    assert (state.glucose, state.faults) == (X, {M})
    frame = harness.display.last_frame
    assert (frame.mg_dl, frame.last_known) == (None, True)
    assert harness.run_until(10).faults == {A, M}
    state = harness.run_until(60)
    assert (state.glucose, state.faults) == (N, set())


def test_clock_is_untrusted_until_the_first_check_completes():
    held: list = []
    runtime = build_runtime(clock_spawn=held.append)
    state = runtime.tick()
    assert (state.glucose, state.faults) == (X, {C})
    held.pop()()
    state = runtime.tick()
    assert (state.glucose, state.faults) == (N, set())


def test_heartbeat_never_received_is_a_communication_fault():
    runtime = build_runtime(transport=SimulatedTransport(heartbeat_ok=lambda t: False))
    assert runtime.tick().faults == {K}


def test_display_failure_does_not_stop_alarm_evaluation():
    harness = Harness(SCENARIOS["hypoglycemia"])
    harness.display.fail = True
    state = harness.run_until(350)
    assert state.glucose is L and harness.transport.last_sent is state
    assert harness.runtime.counts["display_errors"] == 351


def test_transport_send_failure_does_not_stop_alarm_evaluation():
    harness = Harness(SCENARIOS["hypoglycemia"])
    harness.transport.fail = True
    state = harness.run_until(350)
    assert state.glucose is L and harness.display.last_frame.glucose is L
    assert harness.runtime.counts["transport_errors"] == 351


def test_heartbeat_query_failure_is_a_communication_fault():
    class Broken(SimulatedTransport):
        def receive(self, monotonic_s: float) -> Inbound:
            raise OSError("synthetic")

    runtime = build_runtime(transport=Broken(heartbeat_ok=lambda t: True))
    assert runtime.tick().faults == {K}
    assert runtime.counts["transport_errors"] == 1


def test_provider_and_clock_check_crashes_become_faults():
    class Crashing:
        def read(self, now):
            raise RuntimeError("synthetic")

    def broken_clock() -> bool:
        raise OSError("synthetic")

    runtime = build_runtime(provider=Crashing(), clock_check=broken_clock)
    assert runtime.tick().faults == {A, C}
    assert (runtime.counts["fetch_failures"], runtime.counts["clock_check_failures"]) == (1, 1)


def test_engine_defect_propagates_as_a_crash():
    runtime = build_runtime(wall_clock=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        runtime.run(lambda timeout: False)


def test_restart_starts_fail_safe_and_forgets_acknowledgement():
    first = Harness(SCENARIOS["stale_data"])
    first.run_until(1000)
    first.transport.press()
    first.transport.hold()
    assert first.run_until(1001).faults_silenced
    restarted = Harness(SCENARIOS["stale_data"])
    restarted.t = 1002
    state = restarted.run_until(1002)
    assert (state.glucose, state.faults) == (X, {M})
    assert state.fault_audible and not state.faults_silenced
    frame = restarted.display.last_frame
    assert (frame.mg_dl, frame.last_known) == (None, True)


def test_run_stops_promptly_with_a_blocked_fetch():
    release = threading.Event()
    threads: list[threading.Thread] = []
    clock = [0.0]

    def spawn(job):
        thread = threading.Thread(target=job, daemon=True)
        threads.append(thread)
        thread.start()

    class Blocking:
        def read(self, now):
            release.wait()
            return Reading(110, now)

    def wait(timeout: float) -> bool:
        clock[0] += timeout
        return clock[0] >= 120

    runtime = build_runtime(provider=Blocking(), monotonic=lambda: clock[0],
                            acquisition_spawn=spawn)
    runtime.run(wait)
    assert runtime.counts["ticks"] == 120
    assert len(threads) == 1 and threads[0].is_alive()
    assert runtime.counts["fetch_timeouts"] == 1
    assert runtime.machine.state.faults == {A, M}
    release.set()
    threads[0].join(5)


def test_fixed_rate_schedule_skips_missed_slots_without_drift():
    clock = [0.0]
    durations = iter([0.2, 2.5, 0.2, 0.2])
    waits: list[float] = []

    class SlowDisplay(MemoryDisplay):
        def render(self, frame):
            clock[0] += next(durations)

    def wait(timeout: float) -> bool:
        waits.append(round(timeout, 9))
        clock[0] += timeout
        return len(waits) == 4

    runtime = build_runtime(display=SlowDisplay(), monotonic=lambda: clock[0])
    runtime.run(wait)
    assert waits == [0.8, 0.5, 0.8, 0.8]
    assert runtime.counts["overruns"] == 2
