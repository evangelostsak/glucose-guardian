import random
import tomllib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from glucose_guardian.alarms import (
    MAX_ACKNOWLEDGEMENT_S,
    AlarmMachine,
    Fault,
    GlucoseLevel,
    Policy,
)
from glucose_guardian.providers import Reading, SimulatedProvider

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
POLICY = Policy(urgent_low_mg_dl=55, low_mg_dl=70, high_mg_dl=250, hysteresis_mg_dl=5,
                rearm_mg_dl=10, max_age_s=600, max_future_skew_s=60,
                heartbeat_timeout_s=5, acknowledgement_s=60)
HEALTHY = {"mg_dl": 110, "age_s": 0, "tick": 0, "heartbeat_s": 0,
           "acquisition_ok": True, "clock_ok": True, "missing": False}

U, L, N, H, X = (GlucoseLevel.URGENT_LOW, GlucoseLevel.LOW, GlucoseLevel.NORMAL,
                 GlucoseLevel.HIGH, GlucoseLevel.UNKNOWN)


@pytest.fixture
def machine():
    return AlarmMachine(POLICY)


def step(machine, **changes):
    assert set(changes) <= set(HEALTHY)
    i = {**HEALTHY, **changes}
    reading = None if i["missing"] else Reading(i["mg_dl"], NOW - timedelta(seconds=i["age_s"]))
    return machine.update(reading, now=NOW, monotonic_s=i["tick"],
                          acquisition_ok=i["acquisition_ok"],
                          clock_synchronized=i["clock_ok"], heartbeat_age_s=i["heartbeat_s"])


def test_startup_is_unknown_and_faulted(machine):
    state = machine.state
    assert (state.glucose, state.glucose_current, state.faults) == (
        X, False, {Fault.MISSING_DATA})
    assert state.fault_audible and not state.glucose_audible


def test_healthy_reading(machine):
    state = step(machine)
    assert (state.glucose, state.glucose_current, state.faults) == (N, True, set())
    assert not state.glucose_audible and not state.fault_audible


@pytest.mark.parametrize("mg_dl,expected", [
    (39, U), (55, U), (56, L), (70, L), (71, N), (249, N), (250, H), (401, H), (5000, H),
])
def test_classification(machine, mg_dl, expected):
    state = step(machine, mg_dl=mg_dl)
    assert state.glucose is expected
    assert state.glucose_audible == (expected is not N)


@pytest.mark.parametrize("values,expected", [
    ([70, 74, 75, 76], [L, L, L, N]),
    ([50, 58, 60, 61], [U, U, U, L]),
    ([50, 72, 75, 76], [U, L, L, N]),
    ([260, 246, 245, 244], [H, H, H, N]),
    ([71, 70, 71, 70, 71], [N, L, L, L, L]),
    ([76, 75, 249], [N, N, N]),
])
def test_hysteresis_transitions(machine, values, expected):
    assert [step(machine, mg_dl=v).glucose for v in values] == expected


def test_flapping_at_threshold_stays_acknowledged(machine):
    assert step(machine, mg_dl=70).glucose_audible
    machine.acknowledge_glucose(0)
    for tick, mg_dl in enumerate([71, 70, 71, 70], start=1):
        assert step(machine, mg_dl=mg_dl, tick=tick).glucose_silenced


def test_missing_reading(machine):
    state = step(machine, missing=True)
    assert (state.glucose, state.faults) == (X, {Fault.MISSING_DATA})


def test_stale_data_keeps_unresolved_alarm_then_recovers(machine):
    step(machine, mg_dl=50)
    state = step(machine, mg_dl=110, age_s=601)
    assert (state.glucose, state.glucose_current, state.faults) == (
        U, False, {Fault.MISSING_DATA})
    assert state.glucose_audible and state.fault_audible
    state = step(machine, mg_dl=110)
    assert (state.glucose, state.faults) == (N, set())


def test_stale_normal_becomes_unknown(machine):
    step(machine)
    state = step(machine, age_s=601)
    assert (state.glucose, state.glucose_current) == (X, False)


@pytest.mark.parametrize("changes", [{"mg_dl": float("nan")}, {"age_s": -61}])
def test_invalid_reading_then_recovery(machine, changes):
    state = step(machine, **changes)
    assert (state.glucose, state.faults) == (X, {Fault.INVALID_READING})
    assert step(machine).faults == set()


def test_reading_within_future_skew_is_accepted(machine):
    assert step(machine, age_s=-60).faults == set()


def test_unsynchronized_clock_distrusts_readings(machine):
    state = step(machine, clock_ok=False)
    assert (state.glucose, state.glucose_current, state.faults) == (X, False, {Fault.CLOCK})
    step(machine, mg_dl=50)
    assert step(machine, clock_ok=False).glucose is U
    assert step(machine).faults == set()


def test_acquisition_failure_with_fresh_latest_reading(machine):
    state = step(machine, acquisition_ok=False)
    assert (state.glucose, state.glucose_current, state.faults) == (
        N, True, {Fault.ACQUISITION})


@pytest.mark.parametrize("heartbeat_s,faulted", [
    (0, False), (5, False), (5.001, True), (float("nan"), True), (float("inf"), True),
    (-1, True),
])
def test_communication_fault(machine, heartbeat_s, faulted):
    assert (Fault.COMMUNICATION in step(machine, heartbeat_s=heartbeat_s).faults) == faulted


def test_normal_glucose_does_not_clear_communication_fault(machine):
    state = step(machine, heartbeat_s=6)
    assert (state.glucose, state.faults) == (N, {Fault.COMMUNICATION})
    assert step(machine).faults == set()


def test_concurrent_faults_are_all_reported(machine):
    state = step(machine, age_s=601, heartbeat_s=6, acquisition_ok=False)
    assert state.faults == {Fault.MISSING_DATA, Fault.COMMUNICATION, Fault.ACQUISITION}


def test_glucose_acknowledgement_expires(machine):
    step(machine, mg_dl=50)
    machine.acknowledge_glucose(0)
    assert step(machine, mg_dl=50, tick=59).glucose_silenced
    assert step(machine, mg_dl=50, tick=60).glucose_audible


def test_improvement_stays_quiet(machine):
    step(machine, mg_dl=50)
    machine.acknowledge_glucose(0)
    state = step(machine, mg_dl=65, tick=1)
    assert state.glucose is L and state.glucose_silenced


def test_escalation_sounds(machine):
    step(machine, mg_dl=65)
    machine.acknowledge_glucose(0)
    state = step(machine, mg_dl=50, tick=1)
    assert state.glucose is U and state.glucose_audible


@pytest.mark.parametrize("acked,quiet,loud", [(50, 41, 40), (260, 269, 270)])
def test_further_worsening_rearms(machine, acked, quiet, loud):
    step(machine, mg_dl=acked)
    machine.acknowledge_glucose(0)
    assert step(machine, mg_dl=quiet, tick=1).glucose_silenced
    assert step(machine, mg_dl=loud, tick=2).glucose_audible


def test_rearm_baseline_taken_from_first_fresh_reading_after_stale_ack(machine):
    step(machine, mg_dl=50)
    step(machine, mg_dl=50, age_s=601)
    machine.acknowledge_glucose(0)
    assert step(machine, mg_dl=48, tick=1).glucose_silenced
    assert step(machine, mg_dl=39, tick=2).glucose_silenced
    assert step(machine, mg_dl=38, tick=3).glucose_audible


def test_return_to_normal_ends_glucose_event(machine):
    step(machine, mg_dl=65)
    machine.acknowledge_glucose(0)
    step(machine, tick=1)
    assert step(machine, mg_dl=65, tick=2).glucose_audible


def test_acknowledging_quiet_channel_does_nothing(machine):
    step(machine)
    machine.acknowledge_glucose(0)
    machine.acknowledge_faults(0)
    state = step(machine, mg_dl=65, heartbeat_s=6, tick=1)
    assert state.glucose_audible and state.fault_audible


def test_glucose_acknowledgement_does_not_silence_faults(machine):
    step(machine, mg_dl=50, heartbeat_s=6)
    machine.acknowledge_glucose(0)
    state = step(machine, mg_dl=50, heartbeat_s=6, tick=1)
    assert state.glucose_silenced and state.fault_audible


def test_fault_acknowledgement_does_not_silence_glucose(machine):
    step(machine, mg_dl=50, heartbeat_s=6)
    machine.acknowledge_faults(0)
    state = step(machine, mg_dl=50, heartbeat_s=6, tick=1)
    assert state.faults_silenced and state.glucose_audible


def test_fault_acknowledgement_expires(machine):
    step(machine, heartbeat_s=6)
    machine.acknowledge_faults(0)
    assert step(machine, heartbeat_s=6, tick=59).faults_silenced
    assert step(machine, heartbeat_s=6, tick=60).fault_audible


def test_new_fault_sounds(machine):
    step(machine, heartbeat_s=6)
    machine.acknowledge_faults(0)
    assert step(machine, heartbeat_s=6, acquisition_ok=False, tick=1).fault_audible


def test_reappearing_fault_sounds(machine):
    step(machine, heartbeat_s=6, acquisition_ok=False)
    machine.acknowledge_faults(0)
    assert step(machine, heartbeat_s=6, tick=1).faults_silenced
    assert step(machine, heartbeat_s=6, acquisition_ok=False, tick=2).fault_audible


def test_cleared_faults_end_fault_event(machine):
    step(machine, heartbeat_s=6)
    machine.acknowledge_faults(0)
    step(machine, tick=1)
    assert step(machine, heartbeat_s=6, tick=2).fault_audible


def test_acknowledgement_cap_is_fifteen_minutes():
    assert MAX_ACKNOWLEDGEMENT_S == 900
    machine = AlarmMachine(Policy(**{**asdict(POLICY), "acknowledgement_s": 900}))
    step(machine, mg_dl=50)
    machine.acknowledge_glucose(0)
    assert step(machine, mg_dl=50, tick=899.999).glucose_silenced
    assert step(machine, mg_dl=50, tick=900).glucose_audible


@pytest.mark.parametrize("changes", [
    {"urgent_low_mg_dl": 70},
    {"high_mg_dl": 70},
    {"hysteresis_mg_dl": 16},
    {"high_mg_dl": 80},
    {"acknowledgement_s": 901},
    {"rearm_mg_dl": float("nan")},
    {"max_future_skew_s": 0},
    {"hysteresis_mg_dl": -1},
])
def test_invalid_policy(changes):
    with pytest.raises(ValueError):
        Policy(**{**asdict(POLICY), **changes})


def test_example_config_is_a_valid_policy():
    path = Path(__file__).resolve().parents[2] / "config" / "example.toml"
    with path.open("rb") as file:
        assert Policy(**tomllib.load(file)["alarm"])


def test_naive_now_raises(machine):
    with pytest.raises(ValueError):
        machine.update(Reading(110, NOW), now=NOW.replace(tzinfo=None), monotonic_s=0,
                       acquisition_ok=True, clock_synchronized=True, heartbeat_age_s=0)


def test_randomised_invariants():
    rng = random.Random(20261106)
    machine = AlarmMachine(POLICY)
    previous = machine.state
    glucose_ack_at = fault_ack_at = float("-inf")
    tick = 0.0
    for _ in range(5000):
        tick += rng.uniform(1, 40)
        if rng.random() < 0.3:
            machine.acknowledge_glucose(tick)
            if machine.state.glucose_silenced and not previous.glucose_silenced:
                glucose_ack_at = tick
        if rng.random() < 0.3:
            machine.acknowledge_faults(tick)
            if machine.state.faults_silenced and not previous.faults_silenced:
                fault_ack_at = tick
        state = step(
            machine, tick=tick,
            mg_dl=rng.choice([30, 50, 56, 60, 65, 70, 72, 75, 76, 110, 244, 246, 250, 260,
                              float("nan")]),
            age_s=rng.choice([0, 0, 0, 0, 601, -61]),
            heartbeat_s=rng.choice([0, 0, 0, 6]),
            acquisition_ok=rng.random() < 0.9,
            clock_ok=rng.random() < 0.95,
            missing=rng.random() < 0.05)
        if state.glucose_silenced:
            assert tick - glucose_ack_at < POLICY.acknowledgement_s <= MAX_ACKNOWLEDGEMENT_S
        if state.faults_silenced:
            assert tick - fault_ack_at < POLICY.acknowledgement_s
        if state.faults - previous.faults:
            assert state.fault_audible
        if previous.glucose in (N, X) and state.glucose in (U, L, H):
            assert state.glucose_audible
        if state.glucose is N:
            assert state.glucose_current
        previous = state


def test_simulation_is_deterministic():
    provider = SimulatedProvider((100, 50))
    assert [provider.read(NOW).mg_dl for _ in range(3)] == [100, 50, 100]
    assert provider.read(NOW).timestamp == NOW
    with pytest.raises(ValueError):
        SimulatedProvider(())
