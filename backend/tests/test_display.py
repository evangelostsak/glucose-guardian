import logging
from datetime import timedelta

from harness import NOW

from glucose_guardian.alarms import Fault, GlucoseLevel, State
from glucose_guardian.display import LogDisplay, build_frame
from glucose_guardian.providers import Reading

CURRENT = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
STALE = State(GlucoseLevel.URGENT_LOW, False,
              frozenset({Fault.MISSING_DATA, Fault.COMMUNICATION}), False, False)


def test_current_frame():
    frame = build_frame(CURRENT, Reading(123.4, NOW), NOW)
    assert (frame.mg_dl, frame.last_known, frame.data_age_s, frame.faults) == (
        123.4, False, 0.0, ())


def test_stale_frame_is_last_known_with_age_and_sorted_faults():
    frame = build_frame(STALE, Reading(50, NOW), NOW + timedelta(minutes=12))
    assert (frame.glucose, frame.mg_dl, frame.last_known, frame.data_age_s) == (
        GlucoseLevel.URGENT_LOW, 50, True, 720.0)
    assert frame.faults == (Fault.COMMUNICATION, Fault.MISSING_DATA)
    assert frame.glucose_audible and frame.fault_audible


def test_frame_without_any_reading():
    frame = build_frame(STALE, None, NOW)
    assert (frame.mg_dl, frame.data_age_s, frame.last_known) == (None, None, True)


def test_log_display_logs_changes_without_glucose_values(caplog):
    display = LogDisplay()
    with caplog.at_level(logging.INFO, logger="glucose_guardian.display"):
        for state in (CURRENT, CURRENT, STALE):
            display.render(build_frame(state, Reading(123.4, NOW), NOW))
    assert len(caplog.records) == 2
    assert "123" not in caplog.text
    assert "faults=communication,missing_data" in caplog.text
