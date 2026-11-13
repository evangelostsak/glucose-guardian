from dataclasses import replace
from datetime import timedelta

import pytest
from harness import NOW, POLICY, Harness, build_runtime

from glucose_guardian.alarms import Fault
from glucose_guardian.dexcom_share import Trend
from glucose_guardian.display import MemoryDisplay
from glucose_guardian.providers import Reading
from glucose_guardian.simulation import SCENARIOS, Scenario, ScenarioProvider


class Sequence:
    def __init__(self, *readings: Reading) -> None:
        self.readings = list(readings)

    def read(self, now):
        return self.readings.pop(0) if self.readings else None


@pytest.mark.parametrize("newer", [
    Reading(55, NOW - timedelta(seconds=POLICY.max_age_s + 30), Trend.DOUBLE_DOWN),
    Reading(55, NOW + timedelta(seconds=POLICY.max_future_skew_s + 30), Trend.DOUBLE_DOWN),
], ids=["stale", "future-dated"])
def test_a_rejected_newer_reading_never_lends_its_trend_to_the_shown_value(newer):
    clock = [0.0]
    display = MemoryDisplay()
    shown = Reading(120, NOW - timedelta(seconds=60), Trend.SINGLE_UP)
    runtime = build_runtime(provider=Sequence(shown, newer), display=display,
                            monotonic=lambda: clock[0])
    assert runtime.tick().glucose_current
    clock[0] += 60
    state = runtime.tick()
    assert not state.glucose_current
    assert state.faults & {Fault.MISSING_DATA, Fault.INVALID_READING}
    frame = display.last_frame
    assert (frame.mg_dl, frame.trend, frame.reading_time) == (120, Trend.SINGLE_UP,
                                                              shown.timestamp)


def test_a_reading_without_a_trend_shows_none():
    display = MemoryDisplay()
    build_runtime(provider=Sequence(Reading(120, NOW, None)), display=display).tick()
    assert (display.last_frame.mg_dl, display.last_frame.trend) == (120, None)


def test_scenario_readings_carry_their_segment_trend():
    scenario = SCENARIOS["hypoglycemia"]
    elapsed = [0.0]
    provider = ScenarioProvider(scenario, start_wall=NOW, elapsed=lambda: elapsed[0],
                                hang=lambda seconds: None)
    seen = []
    for t in (0, 300, 600, 900):
        elapsed[0] = t
        seen.append(provider.read(NOW + timedelta(seconds=t)).trend)
    assert seen == [Trend.FLAT, Trend.SINGLE_DOWN, Trend.FORTY_FIVE_DOWN, Trend.DOUBLE_UP]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_alarm_results_do_not_depend_on_trends(name):
    scenario = SCENARIOS[name]
    without = Scenario(name, tuple(replace(segment, trend=None)
                                   for segment in scenario.segments))
    end_s = sum(segment.duration_s for segment in scenario.segments)
    states = []
    for variant in (scenario, without):
        harness = Harness(variant)
        trace = []
        while harness.t <= end_s:
            trace.append(harness.run_until(harness.t))
        states.append(trace)
    assert states[0] == states[1]
