"""Bench command line: discover devices, run the bench suite or the watchdog test."""
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
from .checks import HARDWARE, Bench, dtr_reset, run_suite, run_watchdog
from .discovery import BY_ID_DIR, candidates, describe
from .probe import open_bench_port
from .report import Report, Status, Target, write

log = logging.getLogger("glucose_guardian.bench")

OPTIBOOT_FLASH_BYTES = 32768
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


def bootloader_version(dump: Path) -> int:
    data = dump.read_bytes()
    if len(data) != OPTIBOOT_FLASH_BYTES:
        print(f"No Optiboot version marker: dump is {len(data)} bytes. Do not run the watchdog "
              "test on this board.")
        return EXIT_FAILED
    major, minor = data[-1], data[-2]
    print(f"Optiboot version {major}.{minor}")
    if major < 4 or major == 0xFF:
        print("Bootloader too old or unknown for watchdog resets. Do not run the watchdog test.")
        return EXIT_FAILED
    print("OK: bootloader handles watchdog resets.")
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
    dump = commands.add_parser("bootloader-version", help="check an avrdude raw flash dump")
    dump.add_argument("dump", type=Path)
    args = parser.parse_args(argv)
    if args.command == "discover":
        return discover()
    if args.command == "bootloader-version":
        return bootloader_version(args.dump)
    return run(args.command, args.device, args.report_dir,
               args.command == "run" and args.interactive,
               args.flash_record if args.command == "run" else None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
