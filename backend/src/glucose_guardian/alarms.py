"""Pure alarm policy. All inputs, times and thresholds are supplied explicitly."""
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from math import isfinite

from .providers import Reading
from .validation import Validity, require_aware, validate

MAX_ACKNOWLEDGEMENT_S = 15 * 60


class GlucoseLevel(str, Enum):
    URGENT_LOW = "urgent_low"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"


ALARM_LEVELS = frozenset({GlucoseLevel.URGENT_LOW, GlucoseLevel.LOW, GlucoseLevel.HIGH})


class Fault(str, Enum):
    ACQUISITION = "acquisition"
    MISSING_DATA = "missing_data"
    INVALID_READING = "invalid_reading"
    CLOCK = "clock_unsynchronized"
    COMMUNICATION = "communication"
    CONTROLLER_RESET = "controller_reset"
    BUTTON_STUCK = "button_stuck"


CONTROLLER_FAULTS = frozenset({Fault.CONTROLLER_RESET, Fault.BUTTON_STUCK})


@dataclass(frozen=True)
class Policy:
    urgent_low_mg_dl: float
    low_mg_dl: float
    high_mg_dl: float
    hysteresis_mg_dl: float
    rearm_mg_dl: float
    max_age_s: float
    max_future_skew_s: float
    heartbeat_timeout_s: float
    glucose_acknowledgement_s: float
    fault_acknowledgement_s: float

    def __post_init__(self) -> None:
        values = (self.urgent_low_mg_dl, self.low_mg_dl, self.high_mg_dl,
                  self.hysteresis_mg_dl, self.rearm_mg_dl, self.max_age_s,
                  self.max_future_skew_s, self.heartbeat_timeout_s,
                  self.glucose_acknowledgement_s, self.fault_acknowledgement_s)
        if not all(isfinite(v) and v > 0 for v in values):
            raise ValueError("policy values must be finite and positive")
        if not self.urgent_low_mg_dl < self.low_mg_dl < self.high_mg_dl:
            raise ValueError("thresholds must satisfy urgent_low < low < high")
        if self.urgent_low_mg_dl + self.hysteresis_mg_dl > self.low_mg_dl:
            raise ValueError("hysteresis must not extend urgent low past the low threshold")
        if self.low_mg_dl + self.hysteresis_mg_dl >= self.high_mg_dl - self.hysteresis_mg_dl:
            raise ValueError("low and high hysteresis bands must not overlap")
        if max(self.glucose_acknowledgement_s,
               self.fault_acknowledgement_s) > MAX_ACKNOWLEDGEMENT_S:
            raise ValueError(f"acknowledgement durations must not exceed {MAX_ACKNOWLEDGEMENT_S}")


@dataclass(frozen=True)
class State:
    glucose: GlucoseLevel
    glucose_current: bool
    faults: frozenset[Fault]
    glucose_silenced: bool
    faults_silenced: bool

    @property
    def glucose_audible(self) -> bool:
        return self.glucose in ALARM_LEVELS and not self.glucose_silenced

    @property
    def fault_audible(self) -> bool:
        return bool(self.faults) and not self.faults_silenced


@dataclass(frozen=True)
class _GlucoseAck:
    level: GlucoseLevel
    mg_dl: float | None
    until_s: float


@dataclass(frozen=True)
class _FaultAck:
    faults: frozenset[Fault]
    until_s: float


def _classify(mg_dl: float, previous: GlucoseLevel, p: Policy) -> GlucoseLevel:
    if mg_dl <= p.urgent_low_mg_dl:
        return GlucoseLevel.URGENT_LOW
    if (previous is GlucoseLevel.URGENT_LOW
            and mg_dl <= p.urgent_low_mg_dl + p.hysteresis_mg_dl):
        return GlucoseLevel.URGENT_LOW
    if mg_dl <= p.low_mg_dl:
        return GlucoseLevel.LOW
    if (previous in (GlucoseLevel.URGENT_LOW, GlucoseLevel.LOW)
            and mg_dl <= p.low_mg_dl + p.hysteresis_mg_dl):
        return GlucoseLevel.LOW
    if mg_dl >= p.high_mg_dl:
        return GlucoseLevel.HIGH
    if previous is GlucoseLevel.HIGH and mg_dl >= p.high_mg_dl - p.hysteresis_mg_dl:
        return GlucoseLevel.HIGH
    return GlucoseLevel.NORMAL


def _covers(acknowledged: GlucoseLevel, level: GlucoseLevel) -> bool:
    return level is acknowledged or (
        acknowledged is GlucoseLevel.URGENT_LOW and level is GlucoseLevel.LOW)


def _worsening(level: GlucoseLevel, baseline: float, mg_dl: float) -> float:
    return mg_dl - baseline if level is GlucoseLevel.HIGH else baseline - mg_dl


class AlarmMachine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._mg_dl: float | None = None
        self._glucose_ack: _GlucoseAck | None = None
        self._fault_ack: _FaultAck | None = None
        self.state = State(GlucoseLevel.UNKNOWN, False, frozenset({Fault.MISSING_DATA}),
                           False, False)

    def acknowledge_glucose(self, monotonic_s: float) -> None:
        if self.state.glucose_audible:
            self._glucose_ack = _GlucoseAck(
                self.state.glucose, self._mg_dl,
                monotonic_s + self.policy.glucose_acknowledgement_s)
            self.state = replace(self.state, glucose_silenced=True)

    def acknowledge_faults(self, monotonic_s: float) -> None:
        if self.state.fault_audible:
            self._fault_ack = _FaultAck(self.state.faults,
                                        monotonic_s + self.policy.fault_acknowledgement_s)
            self.state = replace(self.state, faults_silenced=True)

    def update(self, reading: Reading | None, *, now: datetime, monotonic_s: float,
               acquisition_ok: bool, clock_synchronized: bool,
               heartbeat_age_s: float, controller_faults: frozenset[Fault]) -> State:
        require_aware(now)
        if not controller_faults <= CONTROLLER_FAULTS:
            raise ValueError(f"unexpected controller faults: {set(controller_faults)}")
        p = self.policy
        faults: set[Fault] = set(controller_faults)
        mg_dl: float | None = None
        if not acquisition_ok:
            faults.add(Fault.ACQUISITION)
        if not clock_synchronized:
            faults.add(Fault.CLOCK)
        elif reading is None:
            faults.add(Fault.MISSING_DATA)
        else:
            validity = validate(reading, now=now, max_age_s=p.max_age_s,
                                max_future_skew_s=p.max_future_skew_s)
            if validity is Validity.VALID:
                mg_dl = reading.mg_dl
            elif validity is Validity.STALE:
                faults.add(Fault.MISSING_DATA)
            else:
                faults.add(Fault.INVALID_READING)
        if not (isfinite(heartbeat_age_s) and 0 <= heartbeat_age_s <= p.heartbeat_timeout_s):
            faults.add(Fault.COMMUNICATION)

        previous = self.state.glucose
        if mg_dl is not None:
            level = _classify(mg_dl, previous, p)
        elif previous in ALARM_LEVELS:
            level = previous
        else:
            level = GlucoseLevel.UNKNOWN
        self._mg_dl = mg_dl
        active = frozenset(faults)
        self.state = State(level, mg_dl is not None, active,
                           self._glucose_silenced(level, mg_dl, monotonic_s),
                           self._faults_silenced(active, monotonic_s))
        return self.state

    def _glucose_silenced(self, level: GlucoseLevel, mg_dl: float | None,
                          monotonic_s: float) -> bool:
        ack = self._glucose_ack
        if ack is None:
            return False
        if monotonic_s >= ack.until_s or not _covers(ack.level, level):
            self._glucose_ack = None
            return False
        if mg_dl is not None:
            if ack.mg_dl is None:
                self._glucose_ack = replace(ack, mg_dl=mg_dl)
            elif _worsening(level, ack.mg_dl, mg_dl) >= self.policy.rearm_mg_dl:
                self._glucose_ack = None
                return False
        return True

    def _faults_silenced(self, faults: frozenset[Fault], monotonic_s: float) -> bool:
        ack = self._fault_ack
        if ack is None:
            return False
        if monotonic_s >= ack.until_s or not faults or not faults <= ack.faults:
            self._fault_ack = None
            return False
        self._fault_ack = replace(ack, faults=faults)
        return True
