"""Bench command line: discovery, bench suite, watchdog test and bootloader authorization."""
import argparse
import json
import logging
import os
import platform
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from serial.tools import list_ports

from ..serial_transport import open_serial
from . import authorization
from .authorization import AuthorizationError
from .checks import HARDWARE, Bench, dtr_reset, run_suite, run_watchdog
from .discovery import BY_ID_DIR, candidates, describe
from .probe import open_bench_port
from .report import Report, Status, Target, write

log = logging.getLogger("glucose_guardian.bench")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover() -> int:
    found = candidates(list_ports.comports(), BY_ID_DIR)
    if not found:
        print("No USB serial devices found.")
    for candidate in found:
        print(f"{candidate.device}\tUSB {candidate.usb_id or '-'}\t"
              f"{'Uno' if candidate.recognized else 'unrecognized'}\t{candidate.description}\t"
              f"{candidate.by_id or '-'}")
    return EXIT_OK


def bootloader(device: str, record: Path, build_dir: Path) -> int:
    tooling = {"harness": version("glucose-guardian"), "avrdude": authorization.avrdude_version()}
    try:
        data = authorization.check_bootloader(device, record, build_dir, authorization.LINUX,
                                              authorization.run_avrdude, tooling, utc_now())
    except AuthorizationError as error:
        print(f"Bootloader check FAILED: {error}\nDo not flash the watchdog-test image.",
              file=sys.stderr)
        return EXIT_FAILED
    usb = data["device"]
    print(f"Optiboot {data['bootloader']['version']} on USB {usb['usb_id']} at port "
          f"{usb['usb_port']} (device {usb['devnum']}), serial number "
          f"{'present' if usb['serial_sha256'] else 'absent'}.")
    print(f"Authorization written to {record}. It is valid for "
          f"{authorization.MAX_AGE_S:g} s and only for this USB connection, host boot and "
          "bench build.")
    return EXIT_OK


def authorize_watchdog(device: str, record: Path, build_dir: Path) -> int:
    try:
        authorization.authorize(device, record, build_dir, authorization.LINUX)
    except AuthorizationError as error:
        print(f"Watchdog-test flashing REFUSED: {error}", file=sys.stderr)
        return EXIT_FAILED
    print("Bootloader authorization matches this board, connection and build.")
    return EXIT_OK


def read_flash_record(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    record: dict[str, Any] = json.loads(path.read_text())
    return record


def run(command: str, device: str, report_dir: Path, interactive: bool,
        flash_record: Path | None) -> int:
    started = utc_now()
    directory = report_dir / f"{command}-{started.replace(':', '')}"
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(directory / "run.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    package_log = logging.getLogger("glucose_guardian")
    package_log.setLevel(logging.INFO)
    package_log.addHandler(handler)
    candidate = describe(device, list_ports.comports(), BY_ID_DIR)
    physical = candidate is not None and candidate.usb_id is not None
    bench = Bench(device=device, physical=physical, profile=HARDWARE,
                  session=int.from_bytes(os.urandom(2), "big"), clock=time.monotonic,
                  sleep=time.sleep, open_port=open_bench_port,
                  reset=lambda port: dtr_reset(port, time.sleep), transport_opener=open_serial,
                  present=os.path.exists, prompt=lambda message: print(f"\n>>> {message}\n",
                                                                       flush=True),
                  interactive=interactive)
    log.info("bench %s started: physical=%s profile=%s", command, physical, HARDWARE.name)
    checks = (run_watchdog(bench) if command == "watchdog"
              else run_suite(bench, candidate, read_flash_record(flash_record)))
    for check in checks:
        log.info("%s %s: expected %s; observed %s", check.status.value, check.id,
                 check.expected, check.observed)
    report = Report(
        command=command, started_utc=started, finished_utc=utc_now(),
        target=Target(candidate.device if candidate else device, physical,
                      candidate.usb_id if candidate else None,
                      candidate.description if candidate else None),
        environment={"python": platform.python_version(), "machine": platform.machine(),
                     "package": version("glucose-guardian"), "profile": HARDWARE.name},
        checks=tuple(checks))
    json_path, markdown_path = write(report, directory)
    package_log.removeHandler(handler)
    handler.close()
    print(f"Verdict: {report.verdict.value}\nReport: {markdown_path}\nJSON: {json_path}")
    return EXIT_FAILED if report.verdict is Status.FAIL else EXIT_OK


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m glucose_guardian.bench")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="list serial devices")
    for name, text in (("run", "run the bench suite against production firmware"),
                       ("watchdog", "run the watchdog test against watchdog-test firmware")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--device", required=True)
        sub.add_argument("--report-dir", required=True, type=Path)
        if name == "run":
            sub.add_argument("--interactive", action="store_true")
            sub.add_argument("--flash-record", type=Path)
    for name, text in (("bootloader", "read the flash read-only and record the bootloader"),
                       ("authorize-watchdog", "check the record before watchdog-test flashing")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--device", required=True)
        sub.add_argument("--record", required=True, type=Path)
        sub.add_argument("--build-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "discover":
        return discover()
    if args.command == "bootloader":
        return bootloader(args.device, args.record, args.build_dir)
    if args.command == "authorize-watchdog":
        return authorize_watchdog(args.device, args.record, args.build_dir)
    return run(args.command, args.device, args.report_dir,
               args.command == "run" and args.interactive,
               args.flash_record if args.command == "run" else None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
