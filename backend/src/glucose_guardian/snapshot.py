"""Display snapshot, schema 1: atomic writer and strict reader. Never logs its contents."""
import contextlib
import json
import math
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from .alarms import Fault, GlucoseLevel
from .dexcom_share import ShareStatus, Trend
from .display import Frame, Source

SCHEMA = 1
MAX_BYTES = 4096
MAX_PROMPT_CHARS = 64
MODE = 0o640
FIELDS = ("schema", "written_monotonic_s", "source", "source_status", "outputs_enabled",
          "max_age_s", "glucose", "glucose_current", "mg_dl", "reading_utc", "data_age_s",
          "trend", "faults", "glucose_audible", "fault_audible", "glucose_silenced",
          "faults_silenced", "prompt")

E = TypeVar("E", bound=Enum)
T = TypeVar("T")


class SnapshotProblem(Exception):
    pass


class SnapshotMissing(SnapshotProblem):
    pass


class SnapshotInvalid(SnapshotProblem):
    pass


@dataclass(frozen=True)
class Snapshot:
    written_monotonic_s: float
    source: Source
    source_status: ShareStatus | None
    outputs_enabled: bool
    max_age_s: float
    glucose: GlucoseLevel
    glucose_current: bool
    mg_dl: float | None
    reading_utc: datetime | None
    data_age_s: float | None
    trend: Trend | None
    faults: tuple[Fault, ...]
    glucose_audible: bool
    fault_audible: bool
    glucose_silenced: bool
    faults_silenced: bool
    prompt: str | None


def encode(frame: Frame, *, written_monotonic_s: float, max_age_s: float) -> bytes:
    document: dict[str, object] = {
        "schema": SCHEMA,
        "written_monotonic_s": written_monotonic_s,
        "source": frame.source.value,
        "source_status": None if frame.source_status is None else frame.source_status.value,
        "outputs_enabled": frame.outputs_enabled,
        "max_age_s": max_age_s,
        "glucose": frame.glucose.value,
        "glucose_current": not frame.last_known,
        "mg_dl": frame.mg_dl,
        "reading_utc": None if frame.reading_time is None
        else frame.reading_time.astimezone(UTC).isoformat(),
        "data_age_s": frame.data_age_s,
        "trend": None if frame.trend is None else frame.trend.value,
        "faults": [fault.value for fault in frame.faults],
        "glucose_audible": frame.glucose_audible,
        "fault_audible": frame.fault_audible,
        "glucose_silenced": frame.glucose_silenced,
        "faults_silenced": frame.faults_silenced,
        "prompt": frame.prompt,
    }
    data = json.dumps(document, allow_nan=False, separators=(",", ":")).encode()
    parse_snapshot(data)
    return data


def write_snapshot(path: Path, data: bytes) -> None:
    partial = path.with_name(path.name + ".partial")
    with contextlib.suppress(FileNotFoundError):
        partial.unlink()
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, MODE)
    try:
        with os.fdopen(descriptor, "wb") as file:
            os.fchmod(file.fileno(), MODE)
            file.write(data)
        os.replace(partial, path)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise


def read_snapshot(path: Path) -> Snapshot:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise SnapshotMissing("no snapshot") from None
    except OSError as error:
        raise SnapshotInvalid(f"snapshot unreadable: {type(error).__name__}") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SnapshotInvalid("snapshot is not a regular file")
        data = b""
        while chunk := os.read(descriptor, MAX_BYTES + 1 - len(data)):
            data += chunk
            if len(data) > MAX_BYTES:
                break
    except OSError as error:
        raise SnapshotInvalid(f"snapshot unreadable: {type(error).__name__}") from None
    finally:
        os.close(descriptor)
    return parse_snapshot(data)


def parse_snapshot(data: bytes) -> Snapshot:
    if len(data) > MAX_BYTES:
        raise SnapshotInvalid(f"snapshot larger than {MAX_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys,
                              parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as error:
        raise SnapshotInvalid(f"snapshot is not valid JSON: {type(error).__name__}") from None
    if not isinstance(document, dict) or set(document) != set(FIELDS):
        raise SnapshotInvalid("snapshot fields differ from schema 1")
    if type(document["schema"]) is not int or document["schema"] != SCHEMA:
        raise SnapshotInvalid("snapshot schema is not 1")
    snapshot = Snapshot(
        written_monotonic_s=_number(document, "written_monotonic_s", minimum=0.0),
        source=_member(document, "source", Source),
        source_status=_optional(document, "source_status", lambda key: _member(
            document, key, ShareStatus)),
        outputs_enabled=_bool(document, "outputs_enabled"),
        max_age_s=_positive(document, "max_age_s"),
        glucose=_member(document, "glucose", GlucoseLevel),
        glucose_current=_bool(document, "glucose_current"),
        mg_dl=_optional(document, "mg_dl", lambda key: _positive(document, key)),
        reading_utc=_optional(document, "reading_utc", lambda key: _utc(document, key)),
        data_age_s=_optional(document, "data_age_s",
                             lambda key: _number(document, key, minimum=-math.inf)),
        trend=_optional(document, "trend", lambda key: _member(document, key, Trend)),
        faults=_faults(document),
        glucose_audible=_bool(document, "glucose_audible"),
        fault_audible=_bool(document, "fault_audible"),
        glucose_silenced=_bool(document, "glucose_silenced"),
        faults_silenced=_bool(document, "faults_silenced"),
        prompt=_optional(document, "prompt", lambda key: _prompt(document, key)),
    )
    _check_consistency(snapshot)
    return snapshot


def _check_consistency(snapshot: Snapshot) -> None:
    reading = (snapshot.mg_dl, snapshot.reading_utc, snapshot.data_age_s)
    rules = (
        (all(item is None for item in reading) or all(item is not None for item in reading),
         "mg_dl, reading_utc and data_age_s must all be present or all null"),
        (snapshot.trend is None or snapshot.mg_dl is not None, "a trend needs a reading"),
        (not snapshot.glucose_current or snapshot.mg_dl is not None,
         "a current glucose level needs a reading"),
        ((snapshot.source is Source.SIMULATED) == (snapshot.source_status is None),
         "source_status must be null exactly for a simulated source"),
    )
    for ok, message in rules:
        if not ok:
            raise SnapshotInvalid(f"snapshot inconsistent: {message}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise SnapshotInvalid("snapshot has duplicate keys")
    return dict(pairs)


def _reject_constant(name: str) -> None:
    raise SnapshotInvalid(f"snapshot contains {name}")


def _optional(document: dict[str, Any], key: str, parse: Callable[[str], T]) -> T | None:
    return None if document[key] is None else parse(key)


def _bool(document: dict[str, Any], key: str) -> bool:
    value = document[key]
    if type(value) is not bool:
        raise SnapshotInvalid(f"snapshot {key} must be a boolean")
    return value


def _number(document: dict[str, Any], key: str, *, minimum: float) -> float:
    value = document[key]
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise SnapshotInvalid(f"snapshot {key} must be a finite number")
    return float(value)


def _positive(document: dict[str, Any], key: str) -> float:
    value = _number(document, key, minimum=0.0)
    if value <= 0:
        raise SnapshotInvalid(f"snapshot {key} must be positive")
    return value


def _member(document: dict[str, Any], key: str, kind: type[E]) -> E:
    value = document[key]
    if not isinstance(value, str) or value not in {member.value for member in kind}:
        raise SnapshotInvalid(f"snapshot {key} is not a known value")
    return kind(value)


def _utc(document: dict[str, Any], key: str) -> datetime:
    value = document[key]
    if not isinstance(value, str):
        raise SnapshotInvalid(f"snapshot {key} must be a string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise SnapshotInvalid(f"snapshot {key} is not an ISO 8601 time") from None
    if moment.utcoffset() != timedelta(0):
        raise SnapshotInvalid(f"snapshot {key} must be in UTC")
    return moment


def _faults(document: dict[str, Any]) -> tuple[Fault, ...]:
    value = document["faults"]
    if not isinstance(value, list):
        raise SnapshotInvalid("snapshot faults must be a list")
    known = {fault.value for fault in Fault}
    if not all(isinstance(item, str) and item in known for item in value):
        raise SnapshotInvalid("snapshot faults contain an unknown value")
    faults = tuple(Fault(item) for item in value)
    if len(set(faults)) != len(faults):
        raise SnapshotInvalid("snapshot faults repeat")
    return faults


def _prompt(document: dict[str, Any], key: str) -> str:
    value = document[key]
    if not isinstance(value, str) or not 0 < len(value) <= MAX_PROMPT_CHARS:
        raise SnapshotInvalid(f"snapshot {key} must be a short string")
    return value


class SnapshotDisplay:
    def __init__(self, *, path: Path, monotonic: Callable[[], float], max_age_s: float) -> None:
        self._path = path
        self._monotonic = monotonic
        self._max_age_s = max_age_s

    def render(self, frame: Frame) -> None:
        write_snapshot(self._path, encode(frame, written_monotonic_s=self._monotonic(),
                                          max_age_s=self._max_age_s))
