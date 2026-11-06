"""Display boundary: a pure frame model plus log and in-memory renderers."""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .alarms import Fault, GlucoseLevel, State
from .providers import Reading

log = logging.getLogger(__name__)

HOLD_PROMPT = "Hold 2 s to silence"


@dataclass(frozen=True)
class Frame:
    glucose: GlucoseLevel
    mg_dl: float | None
    last_known: bool
    data_age_s: float | None
    faults: tuple[Fault, ...]
    glucose_audible: bool
    fault_audible: bool
    glucose_silenced: bool
    faults_silenced: bool
    prompt: str | None


def build_frame(state: State, last_valid: Reading | None, now: datetime) -> Frame:
    return Frame(
        glucose=state.glucose,
        mg_dl=None if last_valid is None else last_valid.mg_dl,
        last_known=not state.glucose_current,
        data_age_s=None if last_valid is None
        else (now - last_valid.timestamp).total_seconds(),
        faults=tuple(sorted(state.faults)),
        glucose_audible=state.glucose_audible,
        fault_audible=state.fault_audible,
        glucose_silenced=state.glucose_silenced,
        faults_silenced=state.faults_silenced,
        prompt=HOLD_PROMPT if state.fault_audible and not state.glucose_audible else None,
    )


class Display(Protocol):
    def render(self, frame: Frame) -> None: ...


class MemoryDisplay:
    def __init__(self) -> None:
        self.last_frame: Frame | None = None
        self.fail = False

    def render(self, frame: Frame) -> None:
        if self.fail:
            raise RuntimeError("simulated display failure")
        self.last_frame = frame


class LogDisplay:
    def __init__(self) -> None:
        self._last: tuple[object, ...] | None = None

    def render(self, frame: Frame) -> None:
        summary = (frame.glucose.value, frame.last_known,
                   ",".join(fault.value for fault in frame.faults) or "none",
                   frame.glucose_silenced, frame.faults_silenced)
        if summary != self._last:
            self._last = summary
            log.info("glucose=%s last_known=%s faults=%s glucose_silenced=%s "
                     "faults_silenced=%s", *summary)
