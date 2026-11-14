"""Synthetic dashboard previews: one PNG per screen state, connection status and level."""
import argparse
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .alarms import Fault, GlucoseLevel
from .config import ConfigError, load_config
from .dexcom_share import ShareStatus, Trend
from .display import HOLD_PROMPT, Source
from .screen_model import ScreenModel, ScreenSettings, classify, restarting
from .screen_render import Renderer
from .snapshot import Snapshot, SnapshotInvalid, SnapshotMissing, SnapshotProblem

log = logging.getLogger("glucose_guardian.preview")

EXIT_OK = 0
EXIT_CONFIG = 2
NOW_LOCAL = datetime(2026, 1, 1, 3, 14, tzinfo=UTC)
NOW_MONOTONIC = 1000.0
MAX_AGE_S = 600.0

LIVE = Snapshot(
    written_monotonic_s=NOW_MONOTONIC - 0.5, source=Source.DEXCOM_SHARE,
    source_status=ShareStatus.OK, outputs_enabled=False, max_age_s=MAX_AGE_S,
    glucose=GlucoseLevel.NORMAL, glucose_current=True, mg_dl=123,
    reading_utc=NOW_LOCAL - timedelta(minutes=4), data_age_s=240.0, trend=Trend.FLAT,
    faults=(), glucose_audible=False, fault_audible=False, glucose_silenced=False,
    faults_silenced=False, prompt=None)


def stale(snapshot: Snapshot, minutes: int, *faults: Fault) -> Snapshot:
    return replace(snapshot, glucose_current=False, data_age_s=minutes * 60.0,
                   reading_utc=NOW_LOCAL - timedelta(minutes=minutes), faults=faults,
                   fault_audible=bool(faults))


def cases(cutoff_s: float) -> list[tuple[str, Snapshot | SnapshotProblem]]:
    cutoff_minutes = int(cutoff_s // 60)
    urgent = replace(LIVE, glucose=GlucoseLevel.URGENT_LOW, mg_dl=48, trend=Trend.SINGLE_DOWN,
                     glucose_audible=True)
    listing: list[tuple[str, Snapshot | SnapshotProblem]] = [
        ("live-normal-flat", LIVE),
        ("live-urgent-low-single-down", urgent),
        ("live-low-forty-five-down", replace(LIVE, glucose=GlucoseLevel.LOW, mg_dl=64,
                                             trend=Trend.FORTY_FIVE_DOWN, glucose_audible=True)),
        ("live-high-double-up", replace(LIVE, glucose=GlucoseLevel.HIGH, mg_dl=262,
                                        trend=Trend.DOUBLE_UP, glucose_audible=True)),
        ("live-normal-forty-five-up", replace(LIVE, mg_dl=141, trend=Trend.FORTY_FIVE_UP)),
        ("live-normal-single-up", replace(LIVE, mg_dl=168, trend=Trend.SINGLE_UP)),
        ("live-normal-no-trend", replace(LIVE, trend=Trend.NOT_COMPUTABLE)),
        ("live-device-low", replace(urgent, mg_dl=39, trend=Trend.DOUBLE_DOWN)),
        ("live-device-high", replace(LIVE, glucose=GlucoseLevel.HIGH, mg_dl=401,
                                     trend=Trend.SINGLE_UP, glucose_audible=True)),
        ("last-known", replace(stale(LIVE, 14, Fault.MISSING_DATA),
                               glucose=GlucoseLevel.UNKNOWN, prompt=HOLD_PROMPT)),
        ("last-known-urgent-low-unresolved", stale(urgent, 12, Fault.MISSING_DATA)),
        ("last-known-urgent-low-silenced", replace(
            stale(urgent, 12, Fault.MISSING_DATA), glucose_audible=False,
            glucose_silenced=True, prompt=HOLD_PROMPT)),
        ("last-known-clock-unsynchronized", replace(stale(LIVE, 3, Fault.CLOCK),
                                                    glucose=GlucoseLevel.UNKNOWN)),
        ("last-known-invalid-reading", replace(stale(LIVE, 6, Fault.INVALID_READING),
                                               glucose=GlucoseLevel.UNKNOWN)),
        ("no-current-data-after-cutoff", replace(
            stale(LIVE, cutoff_minutes + 15, Fault.MISSING_DATA, Fault.ACQUISITION),
            glucose=GlucoseLevel.UNKNOWN, source_status=ShareStatus.NETWORK_ERROR)),
        ("no-current-data-waiting", replace(
            LIVE, glucose=GlucoseLevel.UNKNOWN, glucose_current=False, mg_dl=None,
            reading_utc=None, data_age_s=None, trend=None, faults=(Fault.MISSING_DATA,),
            fault_audible=True, source_status=ShareStatus.LOGIN_DEFERRED)),
        ("no-data-no-recent-data", SnapshotMissing("no snapshot")),
        ("no-data-invalid", SnapshotInvalid("synthetic")),
        ("faults-and-controller", replace(LIVE, faults=(Fault.COMMUNICATION,
                                                        Fault.CONTROLLER_RESET),
                                          fault_audible=True, prompt=HOLD_PROMPT)),
    ]
    for status in ShareStatus:
        connection = replace(LIVE, source_status=status) if status is ShareStatus.OK else \
            replace(stale(LIVE, 9, Fault.ACQUISITION), glucose=GlucoseLevel.UNKNOWN,
                    source_status=status)
        listing.append((f"connection-{status.value.replace('_', '-')}", connection))
    listing.append(("connection-no-response", replace(LIVE, faults=(Fault.ACQUISITION,),
                                                      fault_audible=True, prompt=HOLD_PROMPT)))
    listing.append(("simulated-live", replace(LIVE, source=Source.SIMULATED,
                                              source_status=None)))
    return listing


def models(cutoff_s: float, snapshot_max_age_s: float) -> list[tuple[str, ScreenModel]]:
    rendered = []
    for name, case in cases(cutoff_s):
        source = Source.SIMULATED if name.startswith("simulated") else Source.DEXCOM_SHARE
        settings = ScreenSettings(snapshot_max_age_s=snapshot_max_age_s,
                                  last_known_cutoff_s=cutoff_s, source=source,
                                  outputs_enabled=False)
        rendered.append((name, classify(case, settings=settings, now_monotonic=NOW_MONOTONIC,
                                        now_local=NOW_LOCAL)))
    rendered.append(("display-restarting", restarting()))
    return rendered


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m glucose_guardian.preview")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if config.display is None:
            raise ConfigError(f"{args.config}: [display] kind must be \"snapshot\" for previews")
        renderer = Renderer(config.display.font_file)
    except ConfigError as error:
        log.error("%s", error)
        return EXIT_CONFIG
    except OSError as error:
        log.error("[display] font_file cannot be loaded: %s", type(error).__name__)
        return EXIT_CONFIG
    args.out.mkdir(parents=True, exist_ok=True)
    listing = models(config.display.last_known_cutoff_s, config.display.snapshot_max_age_s)
    for index, (name, model) in enumerate(listing, start=1):
        renderer.render(model).image.save(args.out / f"{index:02d}-{name}.png", format="PNG")
    log.info("wrote %d synthetic previews to %s", len(listing), args.out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
