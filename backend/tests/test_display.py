import logging
from datetime import timedelta

import pytest
from harness import NOW

from glucose_guardian.alarms import Fault, GlucoseLevel, State
from glucose_guardian.dexcom_share import ShareStatus, Trend
from glucose_guardian.display import (
    HOLD_PROMPT,
    FanOutDisplay,
    LogDisplay,
    MemoryDisplay,
    Source,
    build_frame,
)
from glucose_guardian.providers import Reading

CURRENT = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
STALE = State(GlucoseLevel.URGENT_LOW, False,
              frozenset({Fault.MISSING_DATA, Fault.COMMUNICATION}), False, False)
SIMULATED = {"source": Source.SIMULATED, "source_status": None, "outputs_enabled": False}


def frame(state, reading, now, **context):
    return build_frame(state, reading, now, **{**SIMULATED, **context})


def test_current_frame():
    built = frame(CURRENT, Reading(123.4, NOW, Trend.FLAT), NOW)
    assert (built.mg_dl, built.last_known, built.data_age_s, built.faults) == (
        123.4, False, 0.0, ())
    assert (built.reading_time, built.trend) == (NOW, Trend.FLAT)
    assert (built.source, built.source_status, built.outputs_enabled) == (
        Source.SIMULATED, None, False)


def test_stale_frame_is_last_known_with_age_and_sorted_faults():
    built = frame(STALE, Reading(50, NOW, Trend.SINGLE_DOWN), NOW + timedelta(minutes=12))
    assert (built.glucose, built.mg_dl, built.last_known, built.data_age_s) == (
        GlucoseLevel.URGENT_LOW, 50, True, 720.0)
    assert (built.reading_time, built.trend) == (NOW, Trend.SINGLE_DOWN)
    assert built.faults == (Fault.COMMUNICATION, Fault.MISSING_DATA)
    assert built.glucose_audible and built.fault_audible


@pytest.mark.parametrize("state,prompt", [
    (STALE, None),
    (State(GlucoseLevel.URGENT_LOW, False, frozenset({Fault.MISSING_DATA}), True, False),
     HOLD_PROMPT),
    (State(GlucoseLevel.UNKNOWN, False, frozenset({Fault.CLOCK}), False, False), HOLD_PROMPT),
    (State(GlucoseLevel.UNKNOWN, False, frozenset({Fault.CLOCK}), False, True), None),
    (CURRENT, None),
])
def test_hold_prompt_only_when_faults_alone_are_sounding(state, prompt):
    assert frame(state, None, NOW).prompt == prompt


def test_frame_without_any_reading():
    built = frame(STALE, None, NOW)
    assert (built.mg_dl, built.data_age_s, built.last_known) == (None, None, True)
    assert (built.reading_time, built.trend) == (None, None)


def test_dexcom_frame_carries_the_provider_status():
    built = frame(CURRENT, Reading(110, NOW, None), NOW, source=Source.DEXCOM_SHARE,
                  source_status=ShareStatus.AUTH_BLOCKED)
    assert (built.source, built.source_status, built.trend) == (
        Source.DEXCOM_SHARE, ShareStatus.AUTH_BLOCKED, None)


@pytest.mark.parametrize("source,status", [(Source.SIMULATED, ShareStatus.OK),
                                           (Source.DEXCOM_SHARE, None)])
def test_source_and_status_must_agree(source, status):
    with pytest.raises(ValueError, match="status"):
        frame(CURRENT, None, NOW, source=source, source_status=status)


def test_log_display_logs_changes_without_glucose_values(caplog):
    display = LogDisplay()
    with caplog.at_level(logging.INFO, logger="glucose_guardian.display"):
        for state in (CURRENT, CURRENT, STALE):
            display.render(frame(state, Reading(123.4, NOW, Trend.FLAT), NOW))
    assert len(caplog.records) == 2
    assert "123" not in caplog.text
    assert "faults=communication,missing_data" in caplog.text


def test_fan_out_renders_every_display_and_raises_the_first_failure_last():
    broken, second, third = MemoryDisplay(), MemoryDisplay(), MemoryDisplay()
    broken.fail = third.fail = True
    built = frame(CURRENT, None, NOW)
    with pytest.raises(RuntimeError, match="simulated display failure"):
        FanOutDisplay((broken, second, third)).render(built)
    assert second.last_frame == built


def test_fan_out_needs_a_display():
    with pytest.raises(ValueError):
        FanOutDisplay(())
