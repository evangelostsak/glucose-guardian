import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from harness import build_runtime
from share_harness import START, make_provider, synthetic

from glucose_guardian.alarms import Fault, GlucoseLevel
from glucose_guardian.dexcom_share import ShareStatus, Trend
from glucose_guardian.display import MemoryDisplay
from glucose_guardian.share_simulator import Fault as ServerFault
from glucose_guardian.share_simulator import static
from glucose_guardian.worker import Done


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "state.json"


def synchronized() -> bool:
    return True


def runtime_for(provider, clock, *, clock_ok: Callable[[], bool], spawn=None, display=None):
    parts = {"provider": provider, "wall_clock": lambda: clock.wall,
             "monotonic": lambda: clock.boottime, "clock_check": clock_ok}
    if spawn is not None:
        parts["acquisition_spawn"] = spawn
    if display is not None:
        parts["display"] = display
    return build_runtime(**parts)


def run_for(runtime, clock, seconds: int):
    state = runtime.tick()
    for _ in range(seconds):
        clock.advance(1)
        state = runtime.tick()
    return state


def serve(simulator, mg_dl: int, age_s: float) -> None:
    simulator.source = static([synthetic(mg_dl, START - timedelta(seconds=age_s), "Flat")])


def test_fresh_reading_reaches_the_alarm_engine(simulator, state_file, share_clock):
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    state = runtime_for(provider, share_clock, clock_ok=synchronized).tick()
    assert (state.glucose, state.faults) == (GlucoseLevel.NORMAL, frozenset())
    assert provider.status is ShareStatus.OK
    assert not hasattr(provider, "latest_trend")


def test_the_trend_stays_with_the_reading_it_was_sent_with(simulator, state_file,
                                                          share_clock):
    simulator.source = static([synthetic(120, START, "SingleUp")])
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    display = MemoryDisplay()
    runtime = runtime_for(provider, share_clock, clock_ok=synchronized, display=display)
    assert runtime.tick().glucose_current
    assert (display.last_frame.mg_dl, display.last_frame.trend) == (120, Trend.SINGLE_UP)
    share_clock.advance(1300)
    simulator.source = static([synthetic(55, START + timedelta(seconds=600), "DoubleDown")])
    state = run_for(runtime, share_clock, 60)
    assert not state.glucose_current and Fault.MISSING_DATA in state.faults
    assert (display.last_frame.mg_dl, display.last_frame.trend) == (120, Trend.SINGLE_UP)


@pytest.mark.parametrize("mg_dl,level", [(39, GlucoseLevel.URGENT_LOW),
                                         (401, GlucoseLevel.HIGH)])
def test_low_and_high_sentinels_use_the_existing_thresholds(simulator, state_file,
                                                            share_clock, mg_dl, level):
    serve(simulator, mg_dl, 60)
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    assert runtime_for(provider, share_clock, clock_ok=synchronized).tick().glucose is level


def test_stale_duplicates_become_missing_data_and_new_data_recovers(simulator, state_file,
                                                                    share_clock):
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    runtime = runtime_for(provider, share_clock, clock_ok=synchronized)
    assert run_for(runtime, share_clock, 530).faults == frozenset()
    state = run_for(runtime, share_clock, 20)
    assert Fault.MISSING_DATA in state.faults and Fault.ACQUISITION not in state.faults
    assert state.glucose is GlucoseLevel.UNKNOWN
    simulator.source = static([synthetic(120, share_clock.wall, "Flat")])
    state = run_for(runtime, share_clock, 60)
    assert (state.glucose, state.faults) == (GlucoseLevel.NORMAL, frozenset())


def test_outage_is_an_acquisition_fault_then_missing_data_then_recovers(simulator, state_file,
                                                                       share_clock):
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    runtime = runtime_for(provider, share_clock, clock_ok=synchronized)
    runtime.tick()
    simulator.set_fault(ServerFault.SERVER_ERROR, "read")
    state = run_for(runtime, share_clock, 60)
    assert state.faults == frozenset({Fault.ACQUISITION})
    assert provider.status is ShareStatus.SERVER_ERROR
    state = run_for(runtime, share_clock, 600)
    assert state.faults == frozenset({Fault.ACQUISITION, Fault.MISSING_DATA})
    simulator.set_fault(None, "read")
    simulator.source = static([synthetic(110, share_clock.wall, "Flat")])
    state = run_for(runtime, share_clock, 60)
    assert (state.glucose, state.faults) == (GlucoseLevel.NORMAL, frozenset())
    assert provider.status is ShareStatus.OK


def test_future_reading_is_invalid(simulator, state_file, share_clock):
    serve(simulator, 110, -120)
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    state = runtime_for(provider, share_clock, clock_ok=synchronized).tick()
    assert Fault.INVALID_READING in state.faults and state.glucose is GlucoseLevel.UNKNOWN


def test_unsynchronized_clock_prevents_evaluation(simulator, state_file, share_clock):
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    state = runtime_for(provider, share_clock, clock_ok=lambda: False).tick()
    assert Fault.CLOCK in state.faults and state.glucose is GlucoseLevel.UNKNOWN


def test_older_reading_after_a_newer_one_is_ignored(simulator, state_file, share_clock):
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    runtime = runtime_for(provider, share_clock, clock_ok=synchronized)
    runtime.tick()
    serve(simulator, 60, 400)
    state = run_for(runtime, share_clock, 60)
    assert (state.glucose, state.faults) == (GlucoseLevel.NORMAL, frozenset())
    assert provider.status is ShareStatus.OK


def test_blocked_login_is_distinguishable_and_stops_requests(simulator, state_file,
                                                             share_clock):
    simulator.password = "rotated-elsewhere"
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    runtime = runtime_for(provider, share_clock, clock_ok=synchronized)
    state = run_for(runtime, share_clock, 1800)
    assert state.faults == frozenset({Fault.ACQUISITION, Fault.MISSING_DATA})
    assert provider.status is ShareStatus.AUTH_BLOCKED
    assert simulator.requests["authenticate"] == 1
    assert runtime.counts["fetch_failures"] == 31


def test_restart_does_not_log_in_again_within_the_interval(simulator, state_file,
                                                           share_clock):
    first = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                          request_timeout_s=2.0)
    run_for(runtime_for(first, share_clock, clock_ok=synchronized), share_clock, 30)
    restarted = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                              request_timeout_s=2.0)
    runtime = runtime_for(restarted, share_clock, clock_ok=synchronized)
    state = run_for(runtime, share_clock, 200)
    assert Fault.ACQUISITION in state.faults
    assert restarted.status is ShareStatus.LOGIN_DEFERRED
    assert simulator.requests["authenticate"] == 1
    state = run_for(runtime, share_clock, 120)
    assert state.faults == frozenset() and restarted.status is ShareStatus.OK
    assert simulator.requests["authenticate"] == 2


def test_hung_request_keeps_one_worker_thread_and_recovers(simulator, state_file,
                                                           share_clock):
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=5.0)
    spawned: list[threading.Thread] = []

    def spawn(job):
        thread = threading.Thread(target=job, daemon=True)
        spawned.append(thread)
        thread.start()

    runtime = runtime_for(provider, share_clock, clock_ok=synchronized, spawn=spawn)
    runtime.tick()
    spawned[0].join(5)
    share_clock.advance(1)
    assert runtime.tick().faults == frozenset()
    simulator.set_fault(ServerFault.HANG, "read")
    for _ in range(30):
        share_clock.advance(10)
        state = runtime.tick()
    assert len(spawned) == 2
    assert sum(thread.is_alive() for thread in spawned) == 1
    assert Fault.ACQUISITION in state.faults
    assert runtime.counts["fetch_timeouts"] == 1
    simulator.set_fault(None, "read")
    simulator.release.set()
    spawned[-1].join(10)
    assert not spawned[-1].is_alive()
    simulator.source = static([synthetic(110, share_clock.wall, "Flat")])
    deadline = time.monotonic() + 10
    while Fault.ACQUISITION in state.faults or Fault.MISSING_DATA in state.faults:
        assert time.monotonic() < deadline
        share_clock.advance(10)
        state = runtime.tick()
        for thread in spawned:
            thread.join(5)
    assert state.glucose is GlucoseLevel.NORMAL
    assert sum(thread.is_alive() for thread in spawned) == 0


def test_worker_reports_reading_type(simulator, state_file, share_clock):
    provider = make_provider(simulator, state_file, share_clock, boot_id="boot-a",
                             request_timeout_s=2.0)
    outcome = build_runtime(provider=provider).acquisition.poll(
        0.0, lambda: provider.read(share_clock.wall))
    assert isinstance(outcome, Done) and outcome.value is not None
    assert outcome.value.mg_dl == 110
