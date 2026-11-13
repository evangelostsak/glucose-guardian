"""Deterministic simulation scenarios. Values are synthetic, not clinical."""
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from .dexcom_share import Trend
from .providers import Reading


class Fetch(str, Enum):
    OK = "ok"
    ERROR = "error"
    HANG = "hang"


@dataclass(frozen=True)
class Segment:
    duration_s: float
    mg_dl: float | None
    trend: Trend | None
    frozen: bool
    fetch: Fetch
    clock_synchronized: bool
    heartbeat: bool


@dataclass(frozen=True)
class Scenario:
    name: str
    segments: tuple[Segment, ...]

    def segment_at(self, elapsed_s: float) -> tuple[float, Segment]:
        start_s = 0.0
        for segment in self.segments:
            if elapsed_s < start_s + segment.duration_s:
                return start_s, segment
            start_s += segment.duration_s
        last = self.segments[-1]
        return start_s - last.duration_s, last


class SimulatedFetchError(RuntimeError):
    pass


class ScenarioProvider:
    def __init__(self, scenario: Scenario, *, start_wall: datetime,
                 elapsed: Callable[[], float], hang: Callable[[float], None]) -> None:
        self._scenario = scenario
        self._start_wall = start_wall
        self._elapsed = elapsed
        self._hang = hang

    def read(self, now: datetime) -> Reading | None:
        start_s, segment = self._scenario.segment_at(self._elapsed())
        while segment.fetch is Fetch.HANG:
            self._hang(start_s + segment.duration_s - self._elapsed())
            start_s, segment = self._scenario.segment_at(self._elapsed())
        if segment.fetch is Fetch.ERROR:
            raise SimulatedFetchError(self._scenario.name)
        if segment.mg_dl is None:
            return None
        if segment.frozen:
            return Reading(segment.mg_dl, self._start_wall + timedelta(seconds=start_s),
                           segment.trend)
        return Reading(segment.mg_dl, now, segment.trend)


def _ok(duration_s: float, mg_dl: float | None, trend: Trend | None) -> Segment:
    return Segment(duration_s, mg_dl, trend, frozen=False, fetch=Fetch.OK,
                   clock_synchronized=True, heartbeat=True)


def _scenario(name: str, *segments: Segment) -> Scenario:
    return Scenario(name, segments)


FLAT = Trend.FLAT
SCENARIOS = {s.name: s for s in (
    _scenario("normal", _ok(600, 110, FLAT)),
    _scenario("hypoglycemia", _ok(300, 110, FLAT), _ok(300, 65, Trend.SINGLE_DOWN),
              _ok(300, 50, Trend.FORTY_FIVE_DOWN), _ok(600, 110, Trend.DOUBLE_UP)),
    _scenario("hyperglycemia", _ok(300, 110, FLAT), _ok(600, 300, Trend.SINGLE_UP),
              _ok(600, 110, Trend.SINGLE_DOWN)),
    _scenario("stale_data", _ok(300, 50, Trend.FORTY_FIVE_DOWN),
              replace(_ok(1200, 50, Trend.FORTY_FIVE_DOWN), frozen=True),
              _ok(600, 110, Trend.FORTY_FIVE_UP)),
    _scenario("missing_data", _ok(600, None, None), _ok(600, 110, FLAT)),
    _scenario("invalid_readings", _ok(300, 110, FLAT), _ok(300, float("nan"), FLAT),
              _ok(600, 110, FLAT)),
    _scenario("acquisition_timeout", _ok(300, 110, FLAT),
              replace(_ok(300, 110, FLAT), fetch=Fetch.HANG), _ok(600, 110, FLAT)),
    _scenario("acquisition_errors", _ok(300, 110, FLAT),
              replace(_ok(300, 110, FLAT), fetch=Fetch.ERROR), _ok(600, 110, FLAT)),
    _scenario("clock_failure", replace(_ok(300, 110, FLAT), clock_synchronized=False),
              _ok(600, 110, FLAT)),
    _scenario("communication_loss", _ok(300, 110, FLAT),
              replace(_ok(300, 110, FLAT), heartbeat=False), _ok(600, 110, FLAT)),
    _scenario("simultaneous_faults", _ok(300, 50, Trend.FORTY_FIVE_DOWN),
              replace(_ok(1200, 50, Trend.FORTY_FIVE_DOWN), fetch=Fetch.ERROR, heartbeat=False),
              _ok(600, 110, Trend.FORTY_FIVE_UP)),
)}
