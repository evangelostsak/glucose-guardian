"""Read-only Dexcom Share connectivity check. Prints structure and timing, never glucose values."""
import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError, load_config
from .credentials import CredentialsError
from .dexcom_share import (
    DATE,
    ENTRY_KEYS,
    HIGH_MG_DL,
    LEGACY_TRENDS,
    LOW_MG_DL,
    ShareError,
    Trend,
    parse_entries,
    window,
)
from .service import production_session

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
TREND_NAMES = frozenset(trend.value for trend in Trend)


def _instant(value: object) -> datetime | None:
    match = DATE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _known_value(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and LOW_MG_DL <= value <= HIGH_MG_DL


def _known_trend(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return (isinstance(value, str) and value in TREND_NAMES) or (
        isinstance(value, int) and 0 <= value < len(LEGACY_TRENDS))


def _entry(index: int, item: dict[str, object], now: datetime) -> list[str]:
    keys = sorted(item)
    types = ", ".join(f"{key}={type(item[key]).__name__}" for key in keys)
    lines = [f"entry {index}: keys {keys}; types {types}"]
    if set(item) != ENTRY_KEYS:
        lines.append(f"entry {index}: key set differs from {sorted(ENTRY_KEYS)}")
    instants = {key: _instant(item[key]) if key in item else None for key in ("WT", "ST", "DT")}
    wall = instants["WT"]
    if wall is not None:
        lines.append(f"entry {index}: WT age {(now - wall).total_seconds():.0f} s")
        for key in ("ST", "DT"):
            instant = instants[key]
            if instant is not None:
                lines.append(f"entry {index}: {key} minus WT "
                             f"{(instant - wall).total_seconds():+.0f} s")
    display = item["DT"] if "DT" in item else None
    match = DATE.fullmatch(display) if isinstance(display, str) else None
    if match is not None:
        lines.append(f"entry {index}: DT offset {match.group(2)}")
    value = item["Value"] if "Value" in item else None
    trend = item["Trend"] if "Trend" in item else None
    lines.append(f"entry {index}: Value is an integer from {LOW_MG_DL} to {HIGH_MG_DL}: "
                 f"{'yes' if _known_value(value) else 'no'}")
    lines.append(f"entry {index}: Trend recognized: {'yes' if _known_trend(trend) else 'no'}")
    return lines


def describe(raw: bytes, max_count: int, now: datetime) -> tuple[list[str], bool]:
    try:
        items = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ["response: not JSON"], False
    if not isinstance(items, list):
        return [f"response: JSON {type(items).__name__}, not a list"], False
    lines = [f"entries: {len(items)} (requested at most {max_count})"]
    for index, item in enumerate(items):
        if isinstance(item, dict):
            lines.extend(_entry(index, item, now))
        else:
            lines.append(f"entry {index}: JSON {type(item).__name__}, not an object")
    try:
        parse_entries(raw, max_count)
    except ShareError as error:
        lines.append(f"parser: rejected ({error})")
        return lines, False
    lines.append("parser: accepted")
    return lines, True


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m glucose_guardian.share_check")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if config.provider != "dexcom_share":
            raise ConfigError(f"{args.config}: provider must be dexcom_share for this check")
        session = production_session(config)
    except (ConfigError, CredentialsError, OSError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG
    minutes, max_count = window(config.policy.max_age_s)
    try:
        raw = session.read_raw(minutes, max_count)
    except ShareError as error:
        print(f"Dexcom Share check failed: {error.status.value}: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Dexcom Share responded (window {minutes} min, at most {max_count} entries)")
    lines, accepted = describe(raw, max_count, datetime.now(UTC))
    for line in lines:
        print(line)
    return EXIT_OK if accepted else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
