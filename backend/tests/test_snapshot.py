import json
import os
import stat
from datetime import timedelta

import pytest
from harness import NOW

from glucose_guardian import snapshot as snapshot_module
from glucose_guardian.alarms import Fault, GlucoseLevel, State
from glucose_guardian.dexcom_share import ShareStatus, Trend
from glucose_guardian.display import HOLD_PROMPT, Source, build_frame
from glucose_guardian.providers import Reading
from glucose_guardian.snapshot import (
    MAX_BYTES,
    MODE,
    SnapshotDisplay,
    SnapshotInvalid,
    SnapshotMissing,
    encode,
    parse_snapshot,
    read_snapshot,
    write_snapshot,
)

STATE = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
READING = Reading(123, NOW - timedelta(minutes=4), Trend.FORTY_FIVE_UP)


def frame(**context):
    defaults = {"source": Source.DEXCOM_SHARE, "source_status": ShareStatus.OK,
                "outputs_enabled": False}
    return build_frame(STATE, READING, NOW, **{**defaults, **context})


def document(**changes):
    return {**json.loads(encode(frame(), written_monotonic_s=12.5, max_age_s=600)), **changes}


def data(**changes) -> bytes:
    return json.dumps(document(**changes)).encode()


def test_round_trip_keeps_every_field():
    parsed = parse_snapshot(encode(frame(), written_monotonic_s=12.5, max_age_s=600))
    assert (parsed.written_monotonic_s, parsed.source, parsed.source_status) == (
        12.5, Source.DEXCOM_SHARE, ShareStatus.OK)
    assert (parsed.glucose, parsed.glucose_current, parsed.mg_dl, parsed.trend) == (
        GlucoseLevel.NORMAL, True, 123, Trend.FORTY_FIVE_UP)
    assert (parsed.reading_utc, parsed.data_age_s, parsed.max_age_s) == (
        READING.timestamp, 240.0, 600)
    assert (parsed.faults, parsed.prompt, parsed.outputs_enabled) == ((), None, False)


def test_the_largest_frame_fits_the_size_bound():
    state = State(GlucoseLevel.URGENT_LOW, False, frozenset(Fault), True, True)
    built = build_frame(state, READING, NOW, source=Source.DEXCOM_SHARE,
                        source_status=ShareStatus.RATE_LIMITED, outputs_enabled=False)
    encoded = encode(built, written_monotonic_s=1e12, max_age_s=600)
    assert len(encoded) < MAX_BYTES / 4
    assert parse_snapshot(encoded).faults == tuple(sorted(Fault))


def test_simulated_frame_without_a_reading_round_trips():
    state = State(GlucoseLevel.UNKNOWN, False, frozenset({Fault.MISSING_DATA}), False, False)
    built = build_frame(state, None, NOW, source=Source.SIMULATED, source_status=None,
                        outputs_enabled=False)
    parsed = parse_snapshot(encode(built, written_monotonic_s=0, max_age_s=600))
    assert (parsed.source, parsed.source_status, parsed.mg_dl, parsed.reading_utc) == (
        Source.SIMULATED, None, None, None)
    assert parsed.prompt == HOLD_PROMPT


@pytest.mark.parametrize("umask", [0o000, 0o077])
def test_writer_sets_mode_0640_whatever_the_umask(tmp_path, umask):
    path = tmp_path / "display.json"
    previous = os.umask(umask)
    try:
        write_snapshot(path, data())
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == MODE == 0o640
    assert read_snapshot(path).mg_dl == 123


def test_writer_replaces_a_leftover_partial_file(tmp_path):
    path = tmp_path / "display.json"
    partial = tmp_path / "display.json.partial"
    partial.write_text("left by a crash")
    os.chmod(partial, 0o666)
    write_snapshot(path, data())
    assert not partial.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_failed_rename_keeps_the_previous_snapshot_and_removes_the_partial(tmp_path,
                                                                          monkeypatch):
    path = tmp_path / "display.json"
    write_snapshot(path, data(mg_dl=111))

    def refuse(source, target):
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(snapshot_module.os, "replace", refuse)
    with pytest.raises(OSError):
        write_snapshot(path, data(mg_dl=222))
    assert read_snapshot(path).mg_dl == 111
    assert not (tmp_path / "display.json.partial").exists()


def test_writer_never_follows_a_symlinked_target(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    path = tmp_path / "display.json"
    path.symlink_to(victim)
    write_snapshot(path, data())
    assert victim.read_text() == "unchanged" and not path.is_symlink()


def test_snapshot_display_writes_with_the_injected_monotonic_time(tmp_path):
    path = tmp_path / "display.json"
    SnapshotDisplay(path=path, monotonic=lambda: 42.0, max_age_s=600).render(frame())
    assert read_snapshot(path).written_monotonic_s == 42.0


def test_missing_snapshot(tmp_path):
    with pytest.raises(SnapshotMissing):
        read_snapshot(tmp_path / "display.json")


def test_symlink_directory_and_fifo_are_rejected_without_blocking(tmp_path):
    real = tmp_path / "real.json"
    write_snapshot(real, data())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    for path in (link, tmp_path, fifo):
        with pytest.raises(SnapshotInvalid):
            read_snapshot(path)


def test_oversized_snapshot_is_rejected(tmp_path):
    path = tmp_path / "display.json"
    path.write_bytes(data()[:-1] + b" " * MAX_BYTES + b"}")
    with pytest.raises(SnapshotInvalid, match="larger than"):
        read_snapshot(path)


@pytest.mark.parametrize("raw", [
    b"", b"{", b"[]", b"null", b"\xff\xfe", b'{"schema": 1, "schema": 1}',
    b'{"schema": NaN}', b'{"schema": Infinity}',
], ids=["empty", "truncated", "list", "null", "not-utf8", "duplicate-key", "nan", "infinity"])
def test_malformed_bytes_are_rejected(raw):
    with pytest.raises(SnapshotInvalid):
        parse_snapshot(raw)


def test_truncated_snapshot_is_rejected():
    with pytest.raises(SnapshotInvalid):
        parse_snapshot(data()[:-5])


def test_missing_and_extra_fields_are_rejected():
    missing = document()
    del missing["trend"]
    for broken in (missing, {**document(), "mg_dl_raw": 1}):
        with pytest.raises(SnapshotInvalid, match="fields differ"):
            parse_snapshot(json.dumps(broken).encode())


@pytest.mark.parametrize("changes", [
    {"schema": 2}, {"schema": "1"}, {"schema": True}, {"schema": 1.0},
    {"written_monotonic_s": -1}, {"written_monotonic_s": "1"}, {"written_monotonic_s": True},
    {"source": "dexcom"}, {"source": None},
    {"source_status": "online"}, {"source_status": None},
    {"outputs_enabled": 0}, {"outputs_enabled": "false"},
    {"max_age_s": 0}, {"max_age_s": -600},
    {"glucose": "very_low"}, {"glucose": None},
    {"glucose_current": 1},
    {"mg_dl": 0}, {"mg_dl": -5}, {"mg_dl": "123"}, {"mg_dl": True},
    {"reading_utc": "2026-01-01T02:00:00+02:00"}, {"reading_utc": "2026-01-01T00:00:00"},
    {"reading_utc": "yesterday"}, {"reading_utc": 0},
    {"data_age_s": "240"}, {"data_age_s": False},
    {"trend": "Sideways"}, {"trend": 3},
    {"faults": "missing_data"}, {"faults": ["unplugged"]}, {"faults": [1]},
    {"faults": ["missing_data", "missing_data"]},
    {"glucose_audible": 0}, {"fault_audible": None}, {"glucose_silenced": "no"},
    {"faults_silenced": 1},
    {"prompt": ""}, {"prompt": "x" * 65}, {"prompt": 5},
])
def test_ill_typed_or_unknown_values_are_rejected(changes):
    with pytest.raises(SnapshotInvalid):
        parse_snapshot(data(**changes))


@pytest.mark.parametrize("changes", [
    {"mg_dl": None},
    {"reading_utc": None},
    {"data_age_s": None},
    {"mg_dl": None, "reading_utc": None, "data_age_s": None},
    {"source": "simulated"},
    {"source_status": None},
], ids=["value-only-missing", "time-only-missing", "age-only-missing",
        "current-without-reading", "simulated-with-status", "dexcom-without-status"])
def test_inconsistent_snapshots_are_rejected(changes):
    with pytest.raises(SnapshotInvalid, match="inconsistent"):
        parse_snapshot(data(**changes))


def test_trend_without_a_reading_is_rejected():
    changes = {"mg_dl": None, "reading_utc": None, "data_age_s": None,
               "glucose_current": False}
    parse_snapshot(data(**changes, trend=None))
    with pytest.raises(SnapshotInvalid, match="a trend needs a reading"):
        parse_snapshot(data(**changes))


def test_encode_refuses_a_non_finite_value():
    built = build_frame(STATE, Reading(float("nan"), NOW, None), NOW,
                        source=Source.SIMULATED, source_status=None, outputs_enabled=False)
    with pytest.raises(ValueError):
        encode(built, written_monotonic_s=0, max_age_s=600)
