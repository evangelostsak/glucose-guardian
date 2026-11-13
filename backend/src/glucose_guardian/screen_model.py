"""Screen model: what the bedside screen shows for a display snapshot. Pure; no I/O."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .alarms import ALARM_LEVELS, Fault, GlucoseLevel
from .dexcom_share import HIGH_MG_DL, LOW_MG_DL, ShareStatus, Trend
from .display import Source
from .snapshot import Snapshot, SnapshotMissing, SnapshotProblem


class ScreenState(str, Enum):
    NO_DATA = "no_data"
    LIVE = "live"
    LAST_KNOWN = "last_known"
    NO_CURRENT_DATA = "no_current_data"


class Tone(str, Enum):
    TEXT = "text"
    MUTED = "muted"
    GREY = "grey"
    URGENT_LOW = "urgent_low"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    WARNING = "warning"
    ALERT = "alert"


LEVEL_TONES = {
    GlucoseLevel.URGENT_LOW: Tone.URGENT_LOW,
    GlucoseLevel.LOW: Tone.LOW,
    GlucoseLevel.NORMAL: Tone.NORMAL,
    GlucoseLevel.HIGH: Tone.HIGH,
    GlucoseLevel.UNKNOWN: Tone.GREY,
}
LEVEL_LABELS = {
    GlucoseLevel.URGENT_LOW: "URGENT LOW",
    GlucoseLevel.LOW: "LOW",
    GlucoseLevel.NORMAL: "NORMAL",
    GlucoseLevel.HIGH: "HIGH",
    GlucoseLevel.UNKNOWN: "UNKNOWN",
}
FAULT_LABELS = {
    Fault.ACQUISITION: "Data fetch failing",
    Fault.MISSING_DATA: "No current reading",
    Fault.INVALID_READING: "Invalid reading",
    Fault.CLOCK: "Clock not synchronized",
    Fault.COMMUNICATION: "Alarm controller not connected",
    Fault.CONTROLLER_RESET: "Alarm controller restarted",
    Fault.BUTTON_STUCK: "Button stuck",
}
CONNECTIONS = {
    ShareStatus.OK: ("Dexcom: connected", Tone.MUTED),
    ShareStatus.STARTING: ("Dexcom: connecting", Tone.MUTED),
    ShareStatus.LOGIN_DEFERRED: ("Dexcom: waiting to log in", Tone.WARNING),
    ShareStatus.NETWORK_ERROR: ("Dexcom: no connection", Tone.WARNING),
    ShareStatus.SERVER_ERROR: ("Dexcom: server error", Tone.WARNING),
    ShareStatus.PROTOCOL_ERROR: ("Dexcom: unexpected response", Tone.WARNING),
    ShareStatus.RATE_LIMITED: ("Dexcom: rate limited, waiting", Tone.WARNING),
    ShareStatus.AUTH_BLOCKED: ("Dexcom: login blocked, operator action needed", Tone.ALERT),
    ShareStatus.STATE_ERROR: ("Dexcom: login state unavailable", Tone.WARNING),
}
NO_RESPONSE = ("Dexcom: no response", Tone.WARNING)
SIMULATION = ("Source: simulation", Tone.MUTED)
NO_DATA = "NO DATA"
NO_CURRENT_DATA = "NO CURRENT DATA"
LAST_KNOWN = "LAST KNOWN"
NO_RECENT_DATA = "No recent display data"
INVALID = "Display data invalid"
WAITING = "Waiting for the first reading"
NO_CURRENT = "No current reading"
FAULT_REASONS = ((Fault.CLOCK, "Clock not synchronized: readings not evaluated"),
                 (Fault.INVALID_READING, "Latest reading invalid"))
GLUCOSE_SILENCED = "Glucose alarm silenced"
FAULTS_SILENCED = "Fault alarms silenced"
UNIT = "mg/dL"


@dataclass(frozen=True)
class ScreenSettings:
    snapshot_max_age_s: float
    last_known_cutoff_s: float
    source: Source
    outputs_enabled: bool


@dataclass(frozen=True)
class ScreenModel:
    state: ScreenState
    headline: str | None
    detail: str | None
    value: str | None
    value_tone: Tone
    tag: str | None
    trend: Trend | None
    level: str | None
    level_tone: Tone
    age: str | None
    reason: str | None
    connection: str | None
    connection_tone: Tone
    alerts: tuple[str, ...]
    simulated: bool
    display_only: bool
    clock: str | None


def classify(result: Snapshot | SnapshotProblem, *, settings: ScreenSettings,
             now_monotonic: float, now_local: datetime) -> ScreenModel:
    clock = now_local.strftime("%H:%M")
    if isinstance(result, SnapshotProblem):
        return no_data(NO_RECENT_DATA if isinstance(result, SnapshotMissing) else INVALID,
                       settings=settings, clock=clock)
    snapshot = result
    elapsed_s = now_monotonic - snapshot.written_monotonic_s
    if elapsed_s < 0 or snapshot.source is not settings.source \
            or snapshot.outputs_enabled is not settings.outputs_enabled:
        return no_data(INVALID, settings=settings, clock=clock)
    if elapsed_s > settings.snapshot_max_age_s:
        return no_data(NO_RECENT_DATA, settings=settings, clock=clock)
    connection, connection_tone = _connection(snapshot)
    age_s = None if snapshot.data_age_s is None else snapshot.data_age_s + elapsed_s
    mg_dl = snapshot.mg_dl
    if snapshot.glucose_current and mg_dl is not None and age_s is not None \
            and age_s <= snapshot.max_age_s:
        state = ScreenState.LIVE
    elif mg_dl is not None and age_s is not None and age_s <= settings.last_known_cutoff_s:
        state = ScreenState.LAST_KNOWN
    else:
        state = ScreenState.NO_CURRENT_DATA
    reason: str | None = None
    reason_fault: Fault | None = None
    if state is not ScreenState.LIVE:
        reason, reason_fault = _reason(snapshot)
    level, level_tone = _level(snapshot, live=state is ScreenState.LIVE)
    shown_age: str | None = None
    reading_clock = None
    if age_s is not None and snapshot.reading_utc is not None:
        reading_clock = snapshot.reading_utc.astimezone(now_local.tzinfo).strftime("%H:%M")
        shown_age = f"{UNIT} · {format_age(age_s)} ({reading_clock})"
    headline: str | None = None
    detail: str | None = None
    value: str | None = None
    tag: str | None = None
    trend: Trend | None = None
    value_tone = Tone.GREY
    if state is ScreenState.LIVE and mg_dl is not None:
        value = format_value(mg_dl)
        value_tone = LEVEL_TONES[snapshot.glucose]
        trend = snapshot.trend
    elif state is ScreenState.LAST_KNOWN and mg_dl is not None and age_s is not None:
        value = format_value(mg_dl)
        tag = f"{LAST_KNOWN} · {format_age(age_s)}"
        shown_age = f"{UNIT} ({reading_clock})"
    else:
        headline = NO_CURRENT_DATA
        shown_age = None
        if age_s is not None and mg_dl is not None:
            detail = f"Last reading {format_age(age_s)}"
        else:
            detail = reason
            reason = None
    return ScreenModel(
        state=state, headline=headline, detail=detail, value=value, value_tone=value_tone,
        tag=tag, trend=trend, level=level, level_tone=level_tone, age=shown_age,
        reason=reason, connection=connection, connection_tone=connection_tone,
        alerts=_alerts(snapshot, reason_fault), simulated=snapshot.source is Source.SIMULATED,
        display_only=not snapshot.outputs_enabled, clock=clock)


def no_data(detail: str, *, settings: ScreenSettings, clock: str) -> ScreenModel:
    return ScreenModel(
        state=ScreenState.NO_DATA, headline=NO_DATA, detail=detail, value=None,
        value_tone=Tone.GREY, tag=None, trend=None, level=None, level_tone=Tone.GREY, age=None,
        reason=None, connection=None, connection_tone=Tone.MUTED, alerts=(),
        simulated=settings.source is Source.SIMULATED,
        display_only=not settings.outputs_enabled, clock=clock)


def restarting() -> ScreenModel:
    return ScreenModel(
        state=ScreenState.NO_DATA, headline="DISPLAY RESTARTING",
        detail="No glucose data is shown", value=None, value_tone=Tone.GREY, tag=None,
        trend=None, level=None, level_tone=Tone.GREY, age=None, reason=None, connection=None,
        connection_tone=Tone.MUTED, alerts=(), simulated=False, display_only=False, clock=None)


def format_value(mg_dl: float) -> str:
    if mg_dl <= LOW_MG_DL:
        return f"<{LOW_MG_DL + 1}"
    if mg_dl >= HIGH_MG_DL:
        return f">{HIGH_MG_DL - 1}"
    return f"{mg_dl:.0f}"


def format_age(age_s: float) -> str:
    minutes = int(max(age_s, 0.0) // 60)
    if minutes < 1:
        return "<1 min ago"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h {minutes % 60:02d} min ago"


def _connection(snapshot: Snapshot) -> tuple[str, Tone]:
    if snapshot.source_status is None:
        return SIMULATION
    if snapshot.source_status is ShareStatus.OK and Fault.ACQUISITION in snapshot.faults:
        return NO_RESPONSE
    return CONNECTIONS[snapshot.source_status]


def _reason(snapshot: Snapshot) -> tuple[str, Fault | None]:
    for fault, text in FAULT_REASONS:
        if fault in snapshot.faults:
            return text, fault
    missing = Fault.MISSING_DATA if Fault.MISSING_DATA in snapshot.faults else None
    return (WAITING if snapshot.mg_dl is None else NO_CURRENT), missing


def _level(snapshot: Snapshot, *, live: bool) -> tuple[str | None, Tone]:
    if live:
        return LEVEL_LABELS[snapshot.glucose], LEVEL_TONES[snapshot.glucose]
    if snapshot.glucose in ALARM_LEVELS:
        return (f"{LEVEL_LABELS[snapshot.glucose]}: unresolved, no current data",
                LEVEL_TONES[snapshot.glucose])
    return None, Tone.GREY


def _alerts(snapshot: Snapshot, reason_fault: Fault | None) -> tuple[str, ...]:
    alerts = [FAULT_LABELS[fault] for fault in snapshot.faults if fault is not reason_fault]
    if snapshot.glucose_silenced:
        alerts.append(GLUCOSE_SILENCED)
    if snapshot.faults_silenced:
        alerts.append(FAULTS_SILENCED)
    if snapshot.prompt is not None:
        alerts.append(snapshot.prompt)
    return tuple(alerts)
