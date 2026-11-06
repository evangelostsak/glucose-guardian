"""Pure alarm policy. All times and thresholds are supplied explicitly."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite

from .providers import Reading


class GlucoseAlarm(str, Enum):
    NORMAL = "normal"
    LOW = "low"
    CRITICAL = "critical"


class Fault(str, Enum):
    NONE = "none"
    ACQUISITION = "acquisition"
    COMMUNICATION = "communication"
    INVALID = "invalid_reading"
    STALE = "stale_reading"


@dataclass(frozen=True)
class Policy:
    low: float
    critical: float
    max_age_s: float
    heartbeat_timeout_s: float
    acknowledgement_s: float

    def __post_init__(self):
        values = (self.low, self.critical, self.max_age_s,
                  self.heartbeat_timeout_s, self.acknowledgement_s)
        if not all(isfinite(v) and v > 0 for v in values):
            raise ValueError("policy values must be finite and positive")
        if self.critical >= self.low:
            raise ValueError("critical must be below low")


@dataclass(frozen=True)
class State:
    glucose: GlucoseAlarm
    fault: Fault
    acknowledged: bool

    @property
    def audible(self) -> bool:
        return not self.acknowledged and (
            self.glucose != GlucoseAlarm.NORMAL or self.fault != Fault.NONE)


class AlarmMachine:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.state = State(GlucoseAlarm.NORMAL, Fault.ACQUISITION, False)
        self._ack_until = float("-inf")

    def acknowledge(self, monotonic_s: float) -> None:
        if self.state.audible:
            self._ack_until = monotonic_s + self.policy.acknowledgement_s
            self.state = State(self.state.glucose, self.state.fault, True)

    def update(self, reading: Reading | None, *, now: datetime,
               monotonic_s: float, heartbeat_age_s: float,
               acquisition_ok: bool = True) -> State:
        fault = Fault.NONE
        glucose = self.state.glucose
        if not acquisition_ok or reading is None:
            fault = Fault.ACQUISITION
        elif (now.utcoffset() is None or reading.timestamp.utcoffset() is None
              or not isfinite(reading.mg_dl) or reading.mg_dl <= 0):
            fault = Fault.INVALID
        else:
            age = (now - reading.timestamp).total_seconds()
            if age < 0:
                fault = Fault.INVALID
            elif age > self.policy.max_age_s:
                fault = Fault.STALE
            else:
                glucose = (GlucoseAlarm.CRITICAL if reading.mg_dl <= self.policy.critical
                           else GlucoseAlarm.LOW if reading.mg_dl <= self.policy.low
                           else GlucoseAlarm.NORMAL)
        # Retain the last glucose alarm during data failures; never treat stale as normal.
        if (not isfinite(heartbeat_age_s) or heartbeat_age_s < 0
                or heartbeat_age_s > self.policy.heartbeat_timeout_s):
            fault = Fault.COMMUNICATION
        if (glucose, fault) != (self.state.glucose, self.state.fault):
            self._ack_until = float("-inf")
        self.state = State(glucose, fault, monotonic_s < self._ack_until)
        return self.state
