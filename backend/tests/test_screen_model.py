import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glucose_guardian.alarms import ALARM_LEVELS, Fault, GlucoseLevel
from glucose_guardian.dexcom_share import ShareStatus, Trend
from glucose_guardian.display import HOLD_PROMPT, Source
from glucose_guardian.screen_model import (
    CONNECTIONS,
    FAULT_LABELS,
    ScreenSettings,
    ScreenState,
    Tone,
    classify,
    format_age,
    format_value,
    restarting,
)
from glucose_guardian.snapshot import Snapshot, SnapshotInvalid, SnapshotMissing

SOURCE = Path(__file__).resolve().parents[1] / "src" / "glucose_guardian"
NOW_LOCAL = datetime(2026, 1, 1, 3, 14, tzinfo=UTC)
NOW_MONOTONIC = 5000.0
SETTINGS = ScreenSettings(snapshot_max_age_s=5, last_known_cutoff_s=1800,
                          source=Source.DEXCOM_SHARE, outputs_enabled=False)
LIVE = Snapshot(
    written_monotonic_s=NOW_MONOTONIC - 1, source=Source.DEXCOM_SHARE,
    source_status=ShareStatus.OK, outputs_enabled=False, max_age_s=600,
    glucose=GlucoseLevel.NORMAL, glucose_current=True, mg_dl=123,
    reading_utc=NOW_LOCAL - timedelta(minutes=4), data_age_s=239.0, trend=Trend.FLAT,
    faults=(), glucose_audible=False, fault_audible=False, glucose_silenced=False,
    faults_silenced=False, prompt=None)
NO_READING = {"mg_dl": None, "reading_utc": None, "data_age_s": None, "trend": None,
              "glucose_current": False}


def model(snapshot=LIVE, *, settings=SETTINGS, now=NOW_MONOTONIC):
    return classify(snapshot, settings=settings, now_monotonic=now, now_local=NOW_LOCAL)


def stale(age_s: float, **changes):
    return replace(LIVE, glucose_current=False, glucose=GlucoseLevel.UNKNOWN,
                   data_age_s=age_s - 1, faults=(Fault.MISSING_DATA,), fault_audible=True,
                   reading_utc=NOW_LOCAL - timedelta(seconds=age_s), **changes)


def test_live_reading():
    shown = model()
    assert shown.state is ScreenState.LIVE
    assert (shown.value, shown.value_tone, shown.trend) == ("123", Tone.NORMAL, Trend.FLAT)
    assert (shown.level, shown.level_tone) == ("NORMAL", Tone.NORMAL)
    assert shown.age == "mg/dL · 4 min ago (03:10)"
    assert (shown.headline, shown.tag, shown.reason, shown.alerts) == (None, None, None, ())
    assert (shown.connection, shown.clock) == ("Dexcom: connected", "03:14")
    assert shown.display_only and not shown.simulated


@pytest.mark.parametrize("level,tone,label", [
    (GlucoseLevel.URGENT_LOW, Tone.URGENT_LOW, "URGENT LOW"),
    (GlucoseLevel.LOW, Tone.LOW, "LOW"),
    (GlucoseLevel.NORMAL, Tone.NORMAL, "NORMAL"),
    (GlucoseLevel.HIGH, Tone.HIGH, "HIGH"),
])
def test_live_color_comes_from_the_engine_level(level, tone, label):
    shown = model(replace(LIVE, glucose=level))
    assert (shown.value_tone, shown.level_tone, shown.level) == (tone, tone, label)


@pytest.mark.parametrize("data_age_s,state", [
    (599.0, ScreenState.LIVE),
    (599.001, ScreenState.LAST_KNOWN),
], ids=["exactly-max-age", "just-over-max-age"])
def test_freshness_is_recomputed_with_monotonic_time(data_age_s, state):
    shown = model(replace(LIVE, data_age_s=data_age_s))
    assert shown.state is state
    assert (shown.trend is not None) == (state is ScreenState.LIVE)
    assert (shown.value_tone is Tone.GREY) == (state is not ScreenState.LIVE)


def test_last_known_value_is_grey_tagged_and_without_trend():
    shown = model(stale(14 * 60))
    assert shown.state is ScreenState.LAST_KNOWN
    assert (shown.value, shown.value_tone, shown.tag, shown.trend) == (
        "123", Tone.GREY, "LAST KNOWN · 14 min ago", None)
    assert shown.age == "mg/dL (03:00)"
    assert (shown.reason, shown.alerts) == ("No current reading", ())
    assert shown.level is None


@pytest.mark.parametrize("age_s,state", [
    (1800.0, ScreenState.LAST_KNOWN),
    (1800.001, ScreenState.NO_CURRENT_DATA),
], ids=["exactly-cutoff", "just-over-cutoff"])
def test_last_known_cutoff_boundary(age_s, state):
    shown = model(stale(age_s))
    assert shown.state is state
    assert (shown.value is None) == (state is ScreenState.NO_CURRENT_DATA)


def test_after_the_cutoff_only_no_current_data_is_shown():
    shown = model(stale(47 * 60))
    assert (shown.headline, shown.detail) == ("NO CURRENT DATA", "Last reading 47 min ago")
    assert (shown.value, shown.trend, shown.tag, shown.age) == (None, None, None, None)
    assert shown.reason == "No current reading"


def test_no_reading_since_start():
    shown = model(replace(LIVE, **NO_READING, glucose=GlucoseLevel.UNKNOWN,
                          faults=(Fault.MISSING_DATA,), fault_audible=True,
                          prompt=HOLD_PROMPT))
    assert shown.state is ScreenState.NO_CURRENT_DATA
    assert (shown.headline, shown.detail, shown.reason) == (
        "NO CURRENT DATA", "Waiting for the first reading", None)
    assert shown.alerts == (HOLD_PROMPT,)


@pytest.mark.parametrize("now,state", [
    (LIVE.written_monotonic_s + 5, ScreenState.LIVE),
    (LIVE.written_monotonic_s + 5.001, ScreenState.NO_DATA),
], ids=["exactly-snapshot-max-age", "just-over-snapshot-max-age"])
def test_snapshot_expiry_boundary(now, state):
    shown = model(replace(LIVE, data_age_s=0), now=now)
    assert shown.state is state
    if state is ScreenState.NO_DATA:
        assert shown.detail == "Glucose Guardian is not running"


@pytest.mark.parametrize("problem,detail", [
    (SnapshotMissing("x"), "Glucose Guardian is not running"),
    (SnapshotInvalid("x"), "Display data invalid"),
    (replace(LIVE, written_monotonic_s=NOW_MONOTONIC + 0.001), "Display data invalid"),
    (replace(LIVE, source=Source.SIMULATED, source_status=None), "Display data invalid"),
    (replace(LIVE, outputs_enabled=True), "Display data invalid"),
], ids=["missing", "invalid", "written-in-the-future", "other-source", "other-outputs-flag"])
def test_no_data_shows_no_glucose(problem, detail):
    shown = model(problem)
    assert (shown.state, shown.headline, shown.detail) == (ScreenState.NO_DATA, "NO DATA",
                                                           detail)
    assert (shown.value, shown.trend, shown.level, shown.age, shown.connection) == (
        None, None, None, None, None)
    assert shown.display_only and shown.clock == "03:14"


def test_future_reading_within_the_skew_shows_under_a_minute():
    assert model(replace(LIVE, data_age_s=-30.0)).age == "mg/dL · <1 min ago (03:10)"


@pytest.mark.parametrize("status", list(ShareStatus))
def test_every_connection_status_is_shown(status):
    shown = model(replace(stale(9 * 60), source_status=status))
    assert (shown.connection, shown.connection_tone) == CONNECTIONS[status]
    assert shown.state is ScreenState.LAST_KNOWN


def test_auth_blocked_is_an_alert():
    assert CONNECTIONS[ShareStatus.AUTH_BLOCKED][1] is Tone.ALERT
    assert {tone for _, tone in CONNECTIONS.values()} == {Tone.MUTED, Tone.WARNING, Tone.ALERT}


def test_a_hung_fetch_with_an_ok_status_shows_no_response():
    shown = model(replace(LIVE, faults=(Fault.ACQUISITION,), fault_audible=True))
    assert (shown.connection, shown.connection_tone) == ("Dexcom: no response", Tone.WARNING)
    assert shown.alerts == ("Data fetch failing",)


def test_simulated_data_is_always_marked():
    settings = replace(SETTINGS, source=Source.SIMULATED)
    simulated = replace(LIVE, source=Source.SIMULATED, source_status=None)
    for snapshot in (simulated, replace(simulated, written_monotonic_s=0), SnapshotMissing("x")):
        shown = model(snapshot, settings=settings)
        assert shown.simulated
    assert model(simulated, settings=settings).connection == "Source: simulation"
    assert not model(SnapshotMissing("x")).simulated


@pytest.mark.parametrize("fault,reason", [
    (Fault.CLOCK, "Clock not synchronized: readings not evaluated"),
    (Fault.INVALID_READING, "Latest reading invalid"),
])
def test_reason_names_the_fault_that_stops_current_data(fault, reason):
    shown = model(replace(stale(3 * 60), faults=(fault, Fault.ACQUISITION)))
    assert shown.reason == reason
    assert shown.alerts == ("Data fetch failing",)


@pytest.mark.parametrize("level", sorted(ALARM_LEVELS))
@pytest.mark.parametrize("age_s", [12 * 60, 40 * 60])
def test_retained_alarm_level_stays_visible_without_current_data(level, age_s):
    shown = model(replace(stale(age_s), glucose=level))
    assert shown.level.endswith(": unresolved, no current data")
    assert shown.level_tone is model(replace(LIVE, glucose=level)).value_tone
    assert shown.value_tone is Tone.GREY and shown.trend is None


def test_alerts_list_faults_silences_and_the_prompt():
    snapshot = replace(LIVE, faults=(Fault.COMMUNICATION, Fault.CONTROLLER_RESET),
                       fault_audible=False, glucose_silenced=True, faults_silenced=True,
                       prompt=HOLD_PROMPT)
    assert model(snapshot).alerts == ("Alarm controller not connected",
                                      "Alarm controller restarted", "Glucose alarm silenced",
                                      "Fault alarms silenced", HOLD_PROMPT)
    assert set(FAULT_LABELS) == set(Fault)


def test_transitions_between_all_four_states_and_back():
    timeline = [
        (LIVE, ScreenState.LIVE),
        (stale(12 * 60), ScreenState.LAST_KNOWN),
        (stale(31 * 60), ScreenState.NO_CURRENT_DATA),
        (SnapshotMissing("runtime stopped"), ScreenState.NO_DATA),
        (replace(LIVE, **NO_READING, glucose=GlucoseLevel.UNKNOWN), ScreenState.NO_CURRENT_DATA),
        (replace(LIVE, source_status=ShareStatus.AUTH_BLOCKED, **NO_READING,
                 glucose=GlucoseLevel.UNKNOWN), ScreenState.NO_CURRENT_DATA),
        (SnapshotInvalid("corrupt"), ScreenState.NO_DATA),
        (LIVE, ScreenState.LIVE),
    ]
    assert [model(snapshot).state for snapshot, _ in timeline] == [
        state for _, state in timeline]


def test_invariants_across_a_grid_of_snapshots():
    grid = itertools.product(
        list(GlucoseLevel), (True, False), (None, 0.0, 300.0, 599.0, 601.0, 1799.0, 1801.0),
        (0.0, 4.9, 5.1, -0.1), ((), (Fault.MISSING_DATA,), (Fault.CLOCK,)),
        (Trend.DOUBLE_UP, Trend.NONE))
    checked = 0
    for level, current, data_age_s, elapsed_s, faults, trend in grid:
        if data_age_s is None:
            if current:
                continue
            snapshot = replace(LIVE, **NO_READING, glucose=level, faults=faults)
        else:
            snapshot = replace(LIVE, glucose=level, glucose_current=current, faults=faults,
                               data_age_s=data_age_s, trend=trend)
        shown = model(snapshot, now=snapshot.written_monotonic_s + elapsed_s)
        live = shown.state is ScreenState.LIVE
        if shown.trend is not None or (shown.value is not None
                                       and shown.value_tone is not Tone.GREY):
            assert live
        if live:
            assert current and data_age_s is not None and data_age_s + elapsed_s <= 600
        if shown.state in (ScreenState.NO_DATA, ScreenState.NO_CURRENT_DATA):
            assert shown.value is None
        if shown.state is ScreenState.LAST_KNOWN:
            assert data_age_s is not None and data_age_s + elapsed_s <= 1800
        checked += 1
    assert checked > 1000


@pytest.mark.parametrize("mg_dl,shown", [(39, "<40"), (40, "40"), (123.4, "123"),
                                         (400, "400"), (401, ">400")])
def test_device_limits_are_shown_as_ranges(mg_dl, shown):
    assert format_value(mg_dl) == shown


@pytest.mark.parametrize("age_s,shown", [(-30, "<1 min ago"), (0, "<1 min ago"),
                                         (59.9, "<1 min ago"), (60, "1 min ago"),
                                         (3599, "59 min ago"), (3600, "1 h 00 min ago"),
                                         (7500, "2 h 05 min ago")])
def test_age_text(age_s, shown):
    assert format_age(age_s) == shown


def test_restarting_frame_shows_no_glucose_and_no_clock():
    shown = restarting()
    assert (shown.headline, shown.value, shown.clock, shown.trend) == (
        "DISPLAY RESTARTING", None, None, None)


def test_the_cutoff_never_reaches_the_alarm_engine():
    for name in ("alarms.py", "validation.py", "runtime.py", "service.py", "display.py",
                 "snapshot.py"):
        assert "cutoff" not in (SOURCE / name).read_text(), name
