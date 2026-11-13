"""Display boundary: a pure frame model plus log, fan-out and in-memory displays."""
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from .alarms import Fault, GlucoseLevel, State
from .dexcom_share import ShareStatus, Trend
from .providers import Reading

log = logging.getLogger(__name__)

HOLD_PROMPT = "Hold 2 s to silence"


class Source(str, Enum):
    DEXCOM_SHARE = "dexcom_share"
    SIMULATED = "simulated"


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
    reading_time: datetime | None
    trend: Trend | None
    source: Source
    source_status: ShareStatus | None
    outputs_enabled: bool


def build_frame(state: State, last_valid: Reading | None, now: datetime, *, source: Source,
                source_status: ShareStatus | None, outputs_enabled: bool) -> Frame:
    if (source is Source.SIMULATED) != (source_status is None):
        raise ValueError("a Dexcom Share source needs a status; a simulated source has none")
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
        reading_time=None if last_valid is None else last_valid.timestamp,
        trend=None if last_valid is None else last_valid.trend,
        source=source,
        source_status=source_status,
        outputs_enabled=outputs_enabled,
    )


class Display(Protocol):
    def render(self, frame: Frame) -> None: ...


class FanOutDisplay:
    def __init__(self, displays: Sequence[Display]) -> None:
        if not displays:
            raise ValueError("FanOutDisplay needs at least one display")
        self._displays = tuple(displays)

    def render(self, frame: Frame) -> None:
        failure: Exception | None = None
        for display in self._displays:
            try:
                display.render(frame)
            except Exception as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure


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
