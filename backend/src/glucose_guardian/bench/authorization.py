"""Device-bound records: bootloader authorization for flashing the watchdog-test image."""
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .discovery import BY_ID_DIR, redact

SCHEMA = "glucose-guardian/bootloader-authorization/1"
MAX_AGE_S = 900.0
FLASH_BYTES = 32768
OPTIBOOT_BYTES = 512
OPTIBOOT_MIN_MAJOR = 4
AVRDUDE_BASE = ("avrdude", "-p", "atmega328p", "-c", "arduino", "-b", "115200")
AVRDUDE_READ_TIMEOUT_S = 60.0
AVRDUDE_VERSION_TIMEOUT_S = 10.0
RECORD_KEYS = {"schema", "verified_utc", "host_boot_id", "verified_boottime_s", "device",
               "bootloader", "build", "tooling", "integrity_sha256"}


class AuthorizationError(Exception):
    pass


@dataclass(frozen=True)
class Host:
    by_id_dir: Path
    sysfs: Path
    boot_id: Path
    clock: Callable[[], float]


LINUX = Host(by_id_dir=BY_ID_DIR, sysfs=Path("/sys"),
             boot_id=Path("/proc/sys/kernel/random/boot_id"),
             clock=lambda: time.clock_gettime(time.CLOCK_BOOTTIME))


@dataclass(frozen=True)
class UsbDevice:
    by_id: str
    usb_id: str
    usb_port: str
    devnum: int
    serial_sha256: str | None


@dataclass(frozen=True)
class Bootloader:
    classification: str
    version: str
    marker: str
    region_sha256: str
    flash_bytes: int


@dataclass(frozen=True)
class Build:
    commit: str
    sha256sums_sha256: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(body: dict[str, Any]) -> str:
    return _sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as error:
        raise AuthorizationError(f"cannot read {path}: {error}") from error


def _identity(usb: Path) -> tuple[str, str, str | None]:
    serial = usb / "serial"
    return (_read(usb / "idVendor"), _read(usb / "idProduct"),
            (_read(serial) or None) if serial.is_file() else None)


def host_boot_id(host: Host) -> str:
    return _read(host.boot_id)


def _usb_dir(tty: Path, sysfs: Path) -> Path:
    root = sysfs.resolve()
    node = (sysfs / "class" / "tty" / tty.name / "device").resolve()
    for candidate in (node, *node.parents):
        if candidate == root or not candidate.is_relative_to(root):
            break
        if (candidate / "idVendor").is_file() and (candidate / "devnum").is_file():
            return candidate
    raise AuthorizationError(f"{tty} is not a USB device")


def usb_device(device: str, host: Host, boot_id: str) -> UsbDevice:
    path = Path(device)
    if path.parent != host.by_id_dir or not path.is_symlink():
        raise AuthorizationError(f"DEVICE must be a link in {host.by_id_dir}; a name such as "
                                 "/dev/ttyACM0 identifies a port, not a board")
    tty = Path(os.path.realpath(path))
    if not tty.exists():
        raise AuthorizationError(f"{device} does not point to an attached device")
    usb = _usb_dir(tty, host.sysfs)
    identity = _identity(usb)
    try:
        attached = [entry for entry in (host.sysfs / "bus" / "usb" / "devices").iterdir()
                    if (entry / "idVendor").is_file() and _identity(entry) == identity]
    except OSError as error:
        raise AuthorizationError(f"cannot list USB devices: {error}") from error
    if len(attached) != 1:
        raise AuthorizationError(f"{len(attached)} attached USB devices share this board's USB "
                                 "id and serial number; disconnect the others")
    vid, pid, serial = identity
    return UsbDevice(
        by_id=redact(device, serial), usb_id=f"{vid}:{pid}".upper(), usb_port=usb.name,
        devnum=int(_read(usb / "devnum")),
        serial_sha256=None if serial is None else _sha256(f"{boot_id}:{serial}".encode()))


def _optiboot_version(minor: int, major: int) -> str:
    if major == 0xFF or major < OPTIBOOT_MIN_MAJOR:
        raise AuthorizationError(f"bootloader version marker {major}.{minor} is unknown or "
                                 f"older than Optiboot {OPTIBOOT_MIN_MAJOR}")
    return f"{major}.{minor}"


def classify(dump: bytes) -> Bootloader:
    if len(dump) != FLASH_BYTES:
        raise AuthorizationError(f"flash dump is {len(dump)} bytes, not {FLASH_BYTES}: no "
                                 "Optiboot version marker")
    return Bootloader(classification="optiboot", version=_optiboot_version(dump[-2], dump[-1]),
                      marker=dump[-2:].hex(), region_sha256=_sha256(dump[-OPTIBOOT_BYTES:]),
                      flash_bytes=len(dump))


def bench_build(out: Path) -> Build:
    try:
        sums = (out / "SHA256SUMS").read_bytes()
    except OSError as error:
        raise AuthorizationError(f"no bench build in {out}: {error}") from error
    return Build(commit=_read(out / "BUILD-COMMIT"), sha256sums_sha256=_sha256(sums))


def read_command(device: str, dump: Path) -> list[str]:
    return [*AVRDUDE_BASE, "-P", device, "-U", f"flash:r:{dump}:r"]


def run_avrdude(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, timeout=AVRDUDE_READ_TIMEOUT_S, check=False).returncode
    except subprocess.TimeoutExpired as error:
        raise AuthorizationError(
            f"avrdude did not finish within {AVRDUDE_READ_TIMEOUT_S:g} s") from error
    except OSError as error:
        raise AuthorizationError(f"cannot run avrdude: {error}") from error


def avrdude_version() -> str | None:
    try:
        result = subprocess.run(["avrdude", "-?"], capture_output=True, text=True,
                                timeout=AVRDUDE_VERSION_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"avrdude version ([^\s,]+)", result.stdout + result.stderr,
                      re.IGNORECASE)
    return match.group(1) if match else None


def check_bootloader(device: str, record: Path, out: Path, host: Host,
                     read: Callable[[list[str]], int], tooling: dict[str, str | None],
                     utc: str) -> dict[str, Any]:
    record.parent.mkdir(parents=True, exist_ok=True)
    record.unlink(missing_ok=True)
    boot_id = host_boot_id(host)
    before = usb_device(device, host, boot_id)
    build = bench_build(out)
    dump = record.with_name("flash-dump.bin")
    try:
        code = read(read_command(device, dump))
        if code != 0:
            raise AuthorizationError(f"avrdude exited with code {code}")
        try:
            bootloader = classify(dump.read_bytes())
        except OSError as error:
            raise AuthorizationError(f"no flash dump: {error}") from error
    finally:
        dump.unlink(missing_ok=True)
    if usb_device(device, host, boot_id) != before:
        raise AuthorizationError("the USB connection changed during the read; check again")
    return write_record(record, {
        "schema": SCHEMA, "verified_utc": utc, "host_boot_id": boot_id,
        "verified_boottime_s": host.clock(), "device": asdict(before),
        "bootloader": asdict(bootloader), "build": asdict(build), "tooling": tooling})


def write_record(path: Path, body: dict[str, Any]) -> dict[str, Any]:
    data = body | {"integrity_sha256": digest(body)}
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(partial, path)
    return data


def load_record(path: Path, keys: set[str], schema: str, kind: str,
                missing: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise AuthorizationError(missing) from error
    except (OSError, ValueError) as error:
        raise AuthorizationError(f"unreadable {kind} record: {error}") from error
    if not isinstance(data, dict) or set(data) != keys:
        raise AuthorizationError(f"malformed {kind} record")
    body = {key: value for key, value in data.items() if key != "integrity_sha256"}
    if data["integrity_sha256"] != digest(body):
        raise AuthorizationError(f"{kind} record was modified after it was written")
    if data["schema"] != schema:
        raise AuthorizationError(f"unsupported {kind} schema {data['schema']!r}")
    return data


def _differences(recorded: object, current: dict[str, Any], kind: str, section: str) -> list[str]:
    if not isinstance(recorded, dict) or set(recorded) != set(current):
        raise AuthorizationError(f"malformed {kind} record: {section}")
    return [key for key, value in current.items() if recorded[key] != value]


def check_binding(data: dict[str, Any], time_key: str, max_age_s: float, device: str, out: Path,
                  host: Host, kind: str) -> None:
    boot_id = host_boot_id(host)
    if data["host_boot_id"] != boot_id:
        raise AuthorizationError(f"{kind} record is from an earlier boot of this host")
    recorded = data[time_key]
    if type(recorded) is not float:
        raise AuthorizationError(f"malformed {kind} record: {time_key}")
    age = host.clock() - recorded
    if not 0 <= age <= max_age_s:
        raise AuthorizationError(f"{kind} record age {age:.0f} s is outside 0 to "
                                 f"{max_age_s:g} s")
    differing = _differences(data["device"], asdict(usb_device(device, host, boot_id)), kind,
                             "device")
    if differing:
        raise AuthorizationError(f"{kind} record is for another board or connection "
                                 f"({', '.join(differing)} differ)")
    if _differences(data["build"], asdict(bench_build(out)), kind, "build"):
        raise AuthorizationError(f"the bench build changed since the {kind} record")


def _check_bootloader_entry(entry: object) -> None:
    if not isinstance(entry, dict) or set(entry) != {item.name for item in fields(Bootloader)}:
        raise AuthorizationError("malformed authorization record: bootloader")
    try:
        marker = bytes.fromhex(entry["marker"])
    except (TypeError, ValueError) as error:
        raise AuthorizationError("malformed authorization record: bootloader marker") from error
    if (entry["classification"] != "optiboot" or entry["flash_bytes"] != FLASH_BYTES
            or len(marker) != 2 or entry["version"] != _optiboot_version(marker[0], marker[1])):
        raise AuthorizationError("authorization does not show a supported bootloader")


def authorize(device: str, record: Path, out: Path, host: Host) -> dict[str, Any]:
    data = load_record(record, RECORD_KEYS, SCHEMA, "authorization",
                       f"no bootloader authorization at {record}; run 'make bootloader' for "
                       "this board first")
    check_binding(data, "verified_boottime_s", MAX_AGE_S, device, out, host, "authorization")
    _check_bootloader_entry(data["bootloader"])
    return data
