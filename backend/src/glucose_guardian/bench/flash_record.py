"""Device-bound flash records and their verification before the watchdog test."""
import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .authorization import (
    AuthorizationError,
    Host,
    bench_build,
    check_binding,
    host_boot_id,
    load_record,
    usb_device,
    write_record,
)

SCHEMA = "glucose-guardian/flash-record/1"
MAX_AGE_S = 900.0
IMAGES = {"production": "production.hex", "watchdog-test": "watchdog-test-DO-NOT-DEPLOY.hex"}
RECORD_KEYS = {"schema", "variant", "hex", "sha256", "avrdude_exit_code", "utc", "host_boot_id",
               "flashed_boottime_s", "device", "build", "integrity_sha256"}
SUMS_LINE = re.compile(r"([0-9a-f]{64})  (\S+)")


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise AuthorizationError(f"cannot read {path}: {error}") from error


def expected_sha256(out: Path, image: str) -> str:
    try:
        lines = (out / "SHA256SUMS").read_text().splitlines()
    except (OSError, ValueError) as error:
        raise AuthorizationError(f"cannot read {out / 'SHA256SUMS'}: {error}") from error
    entries: dict[str, str] = {}
    for line in lines:
        match = SUMS_LINE.fullmatch(line)
        if match is None or match.group(2) in entries:
            raise AuthorizationError(f"malformed {out / 'SHA256SUMS'}")
        entries[match.group(2)] = match.group(1)
    if image not in entries:
        raise AuthorizationError(f"{out / 'SHA256SUMS'} does not list {image}")
    if file_sha256(out / image) != entries[image]:
        raise AuthorizationError(f"{out / image} does not match SHA256SUMS")
    return entries[image]


def write_flash_record(device: str, variant: str, image: Path, out: Path, exit_code: int,
                       utc: str, record: Path, host: Host) -> dict[str, Any]:
    if IMAGES.get(variant) != image.name:
        raise AuthorizationError(f"{image.name} is not the {variant} image")
    boot_id = host_boot_id(host)
    return write_record(record, {
        "schema": SCHEMA, "variant": variant, "hex": image.name, "sha256": file_sha256(image),
        "avrdude_exit_code": exit_code, "utc": utc, "host_boot_id": boot_id,
        "flashed_boottime_s": host.clock(), "device": asdict(usb_device(device, host, boot_id)),
        "build": asdict(bench_build(out))})


def verify_watchdog_flash(device: str, record: Path, out: Path, host: Host) -> dict[str, Any]:
    data = load_record(record, RECORD_KEYS, SCHEMA, "flash",
                       f"no flash record at {record}; flash the watchdog-test image with "
                       "'make flash' first")
    image = IMAGES["watchdog-test"]
    if data["variant"] != "watchdog-test" or data["hex"] != image:
        raise AuthorizationError(f"the last flash was {data['variant']!r} ({data['hex']!r}), "
                                 "not the watchdog-test image")
    code = data["avrdude_exit_code"]
    if type(code) is not int or code != 0:
        raise AuthorizationError(f"the last flash did not succeed (avrdude exit {code!r})")
    if data["sha256"] != expected_sha256(out, image):
        raise AuthorizationError("the flashed image differs from this build's watchdog-test "
                                 "image")
    check_binding(data, "flashed_boottime_s", MAX_AGE_S, device, out, host, "flash")
    return data
