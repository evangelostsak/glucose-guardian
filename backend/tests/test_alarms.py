from datetime import datetime, timedelta, timezone

import pytest

from glucose_guardian.alarms import AlarmMachine, Fault, GlucoseAlarm, Policy
from glucose_guardian.providers import Reading, SimulatedProvider

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def machine():
    return AlarmMachine(Policy(70, 55, 600, 5, 60))


def update(machine, value=110, tick=0, age=0, heartbeat=0, **kwargs):
    return machine.update(Reading(value, NOW - timedelta(seconds=age)),
                          now=NOW, monotonic_s=tick,
                          heartbeat_age_s=heartbeat, **kwargs)


def test_transitions_and_recovery(machine):
    for value, expected in [(110, GlucoseAlarm.NORMAL), (70, GlucoseAlarm.LOW),
                            (55, GlucoseAlarm.CRITICAL), (110, GlucoseAlarm.NORMAL)]:
        state = update(machine, value)
        assert state.glucose == expected
        assert state.audible == (expected != GlucoseAlarm.NORMAL)


def test_acknowledgement_expires(machine):
    update(machine, 50)
    machine.acknowledge(0)
    assert not update(machine, 50, tick=59).audible
    assert update(machine, 50, tick=60).audible


def test_escalation_and_new_event_reset_ack(machine):
    update(machine, 65)
    machine.acknowledge(0)
    assert update(machine, 50, tick=1).audible
    machine.acknowledge(1)
    update(machine, 110, tick=2)
    assert update(machine, 50, tick=3).audible


@pytest.mark.parametrize('age,fault', [(600, Fault.NONE), (601, Fault.STALE),
                                      (-1, Fault.INVALID)])
def test_timestamp_validation(machine, age, fault):
    assert update(machine, age=age).fault == fault


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 0, -1])
def test_invalid_values(machine, value):
    assert update(machine, value).fault == Fault.INVALID


def test_naive_timestamp(machine):
    state = machine.update(Reading(100, NOW.replace(tzinfo=None)), now=NOW,
                           monotonic_s=0, heartbeat_age_s=0)
    assert state.fault == Fault.INVALID


def test_stale_retains_alarm_and_recovers(machine):
    update(machine, 50)
    state = update(machine, 110, age=601)
    assert state.fault == Fault.STALE
    assert state.glucose == GlucoseAlarm.CRITICAL
    assert state.audible
    assert update(machine).fault == Fault.NONE
    assert machine.state.glucose == GlucoseAlarm.NORMAL


def test_communication_failure_and_recovery(machine):
    assert update(machine, heartbeat=5).fault == Fault.NONE
    assert update(machine, heartbeat=6).fault == Fault.COMMUNICATION
    machine.acknowledge(0)
    assert update(machine, heartbeat=6, tick=60).audible
    assert update(machine, tick=61).fault == Fault.NONE


def test_acquisition_failure_and_recovery(machine):
    state = machine.update(None, now=NOW, monotonic_s=0, heartbeat_age_s=0)
    assert state.fault == Fault.ACQUISITION
    assert state.audible
    assert update(machine).fault == Fault.NONE
    assert update(machine, acquisition_ok=False).fault == Fault.ACQUISITION


def test_new_fault_breaks_ack(machine):
    update(machine, 50)
    machine.acknowledge(0)
    assert update(machine, 50, tick=1, heartbeat=6).audible


def test_simulation_is_deterministic():
    provider = SimulatedProvider((100, 50))
    assert [provider.read(NOW).mg_dl for _ in range(3)] == [100, 50, 100]
    assert provider.read(NOW).timestamp == NOW
    with pytest.raises(ValueError):
        SimulatedProvider(())


@pytest.mark.parametrize('policy', [(55, 70, 600, 5, 60), (70, 55, 0, 5, 60),
                                    (70, 55, 600, 5, float('nan'))])
def test_invalid_policy(policy):
    with pytest.raises(ValueError):
        Policy(*policy)


def test_startup_is_not_healthy(machine):
    assert machine.state.fault == Fault.ACQUISITION
    assert machine.state.audible
