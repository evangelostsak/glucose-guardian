import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest
import serial
from harness import ROOT, FakeUno, pump
from serial.tools.list_ports_common import ListPortInfo

from glucose_guardian.bench import __main__ as cli
from glucose_guardian.bench import checks
from glucose_guardian.bench.authorization import AuthorizationError
from glucose_guardian.bench.checks import (
    Bench,
    Profile,
    check_replug,
    run_suite,
    run_watchdog,
    run_watchdog_verified,
)
from glucose_guardian.bench.discovery import candidates, describe
from glucose_guardian.bench.probe import WATCHDOG_TEST_BANNER, open_bench_port
from glucose_guardian.bench.report import Check, Report, Status, Target, to_markdown
from glucose_guardian.protocol import ResetCause
from glucose_guardian.serial_transport import PyserialPort, open_serial

FAST = Profile(
    name="fast", boot_timeout_s=2, reply_timeout_s=0.3, exchange_interval_s=0.01, exchanges=10,
    silence_window_s=0.15, heartbeat_pause_s=0.2, resets=3, burst_exchanges=30,
    sustained_s=1.0, sustained_tick_s=0.05, max_tick_lateness_s=0.25, backoff_initial_s=0.2,
    backoff_max_s=0.8, unplugged_s=2.5, replug_timeout_s=3, watchdog_cycles=2,
    watchdog_cycle_timeout_s=3, watchdog_gap_min_s=0.05, watchdog_gap_max_s=1.5)
BANNER_LINE = WATCHDOG_TEST_BANNER + b"\n"


class Emulator:
    def __init__(self, master: int, uno: FakeUno) -> None:
        self.master = master
        self.uno = uno
        self.lock = threading.Lock()
        self.hook: Callable[[float], None] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                if self.hook is not None:
                    self.hook(time.monotonic())
                try:
                    pump(self.master, self.uno)
                except OSError:
                    return
            time.sleep(0.001)

    def reboot(self, cause: ResetCause) -> None:
        with self.lock:
            self.uno.reboot((self.uno.boot + 1) % 0x10000, cause)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)


@pytest.fixture
def emulator() -> Iterator[tuple[Emulator, str]]:
    master, slave = os.openpty()
    os.set_blocking(master, False)
    emulator = Emulator(master, FakeUno(0x00A1))
    yield emulator, os.ttyname(slave)
    emulator.close()
    os.close(master)
    os.close(slave)


def make_bench(path: str, emulator: Emulator, profile: Profile, **changes: object) -> Bench:
    bench = Bench(device=path, physical=False, profile=profile, session=0x1234,
                  clock=time.monotonic, sleep=time.sleep, open_port=open_bench_port,
                  reset=lambda port: emulator.reboot(ResetCause.EXTERNAL),
                  transport_opener=open_serial, present=os.path.exists, prompt=print,
                  interactive=False)
    return replace(bench, **changes)


def statuses(results: list[Check]) -> dict[str, Status]:
    return {check.id: check.status for check in results}


def test_suite_against_emulator_passes_and_is_marked_simulated(emulator):
    emu, path = emulator
    results = run_suite(make_bench(path, emu, FAST), None, None)
    by_id = statuses(results)
    passed = {"gg1.handshake", "firmware.identity", "gg1.exchange", "gg1.session_change",
              "gg1.rejection", "link.heartbeat_pause", "reset.unexpected", "reset.repeated",
              "traffic.burst", "traffic.sustained"}
    assert {key for key, status in by_id.items() if status is Status.PASS} == passed
    assert by_id["usb.enumeration"] is Status.NOT_TESTED
    assert by_id["link.uno_alarm"] is Status.NOT_TESTED
    assert by_id["usb.backoff"] is Status.NOT_TESTED
    assert all(check.evidence == "simulated" for check in results
               if check.status is Status.PASS)
    report = Report("run", "start", "end", Target(path, False, None, None), {},
                    tuple(results))
    assert report.verdict is Status.PASS
    assert "SIMULATED TARGET — NOT HARDWARE VALIDATION" in to_markdown(report)


def test_firmware_that_answers_replays_fails_rejection(emulator):
    emu, path = emulator
    emu.uno.accept_replays = True
    by_id = statuses(run_suite(make_bench(path, emu, replace(FAST, sustained_s=0.2)),
                               None, None))
    assert by_id["gg1.rejection"] is Status.FAIL


def test_corrupted_replies_fail_the_handshake_and_skip_link_checks(emulator):
    emu, path = emulator
    emu.uno.corrupt = True
    results = run_suite(make_bench(path, emu, replace(FAST, boot_timeout_s=0.5)), None, None)
    by_id = statuses(results)
    assert by_id["gg1.handshake"] is Status.FAIL
    assert by_id["gg1.exchange"] is Status.NOT_TESTED
    assert by_id["traffic.sustained"] is Status.NOT_TESTED


def test_reset_without_boot_counter_increment_fails(emulator):
    emu, path = emulator

    def stuck_counter(port: serial.Serial) -> None:
        with emu.lock:
            emu.uno.reboot(emu.uno.boot, ResetCause.EXTERNAL)

    bench = make_bench(path, emu, replace(FAST, boot_timeout_s=0.5, sustained_s=0.2),
                       reset=stuck_counter)
    by_id = statuses(run_suite(bench, None, None))
    assert by_id["reset.unexpected"] is Status.FAIL


def banner_hook(emu: Emulator) -> Callable[[float], None]:
    last = [0.0]

    def hook(now: float) -> None:
        if now - last[0] >= 0.1:
            last[0] = now
            emu.uno.outbox += BANNER_LINE

    return hook


def test_watchdog_banner_during_a_production_run_fails_identity(emulator):
    emu, path = emulator
    with emu.lock:
        emu.hook = banner_hook(emu)
    by_id = statuses(run_suite(make_bench(path, emu, FAST), None, None))
    assert by_id["firmware.identity"] is Status.FAIL
    assert by_id["gg1.exchange"] is Status.NOT_TESTED


def test_missing_device_fails_enumeration_only(tmp_path):
    bench = Bench(device=str(tmp_path / "ttyACM9"), physical=False, profile=FAST, session=1,
                  clock=time.monotonic, sleep=time.sleep, open_port=open_bench_port,
                  reset=lambda port: None, transport_opener=open_serial,
                  present=os.path.exists, prompt=print, interactive=False)
    by_id = statuses(run_suite(bench, None, None))
    assert by_id["usb.enumeration"] is Status.FAIL
    assert by_id["gg1.handshake"] is Status.NOT_TESTED


def watchdog_firmware(emu: Emulator, cause: ResetCause) -> None:
    banner = banner_hook(emu)
    state = {"boot_at": time.monotonic(), "hung_at": None}

    def hook(now: float) -> None:
        if state["hung_at"] is None:
            banner(now)
        if state["hung_at"] is None and now - state["boot_at"] > 0.5:
            state["hung_at"] = now
            emu.uno.silent = True
        elif state["hung_at"] is not None and now - state["hung_at"] > 0.2:
            emu.uno.silent = False
            emu.uno.reboot((emu.uno.boot + 1) % 0x10000, cause)
            state.update(boot_at=now, hung_at=None)

    with emu.lock:
        emu.hook = hook


def test_watchdog_command_passes_with_emulated_watchdog_firmware(emulator):
    emu, path = emulator
    watchdog_firmware(emu, ResetCause.WATCHDOG)
    by_id = statuses(run_watchdog(make_bench(path, emu, FAST)))
    assert by_id == {"watchdog.identity": Status.PASS, "watchdog.reset": Status.PASS,
                     "watchdog.cause": Status.PASS}


def test_watchdog_unknown_cause_fails_only_the_cause_check(emulator):
    emu, path = emulator
    watchdog_firmware(emu, ResetCause.UNKNOWN)
    by_id = statuses(run_watchdog(make_bench(path, emu, FAST)))
    assert by_id["watchdog.reset"] is Status.PASS
    assert by_id["watchdog.cause"] is Status.FAIL


def test_watchdog_command_refuses_production_firmware(emulator):
    emu, path = emulator
    by_id = statuses(run_watchdog(make_bench(path, emu, FAST)))
    assert by_id == {"watchdog.identity": Status.FAIL, "watchdog.reset": Status.NOT_TESTED,
                     "watchdog.cause": Status.NOT_TESTED}


def never_open(device: str) -> serial.Serial:
    raise AssertionError(f"port {device} opened")


def test_watchdog_gate_refuses_before_opening_the_port(tmp_path):
    bench = Bench(device=str(tmp_path / "ttyACM9"), physical=False, profile=FAST, session=1,
                  clock=time.monotonic, sleep=time.sleep, open_port=never_open,
                  reset=lambda port: None, transport_opener=open_serial,
                  present=lambda device: True, prompt=print, interactive=False)

    def refuse() -> dict[str, object]:
        raise AuthorizationError("no flash record at last-flash.json")

    results = run_watchdog_verified(bench, refuse)
    assert statuses(results) == {"watchdog.flash_record": Status.FAIL,
                                 "watchdog.identity": Status.NOT_TESTED,
                                 "watchdog.reset": Status.NOT_TESTED,
                                 "watchdog.cause": Status.NOT_TESTED}
    assert results[0].observed == "no flash record at last-flash.json"


def test_watchdog_gate_runs_the_test_after_verification(emulator):
    emu, path = emulator
    watchdog_firmware(emu, ResetCause.WATCHDOG)
    record = {"sha256": "ab" * 32, "utc": "20261107T100500Z"}
    results = run_watchdog_verified(make_bench(path, emu, FAST), lambda: record)
    assert all(check.status is Status.PASS for check in results) and len(results) == 4
    assert results[0].id == "watchdog.flash_record" and results[0].details == record


def test_cli_watchdog_without_flash_evidence_fails_without_opening_the_port(tmp_path,
                                                                             monkeypatch):
    monkeypatch.setattr(cli, "open_bench_port", never_open)
    code = cli.main(["watchdog", "--device", "/dev/serial/by-id/usb-not-attached",
                     "--report-dir", str(tmp_path), "--flash-record",
                     str(tmp_path / "last-flash.json"), "--build-dir", str(tmp_path / "out")])
    assert code == cli.EXIT_FAILED
    (run_dir,) = tmp_path.iterdir()
    data = json.loads((run_dir / "report.json").read_text())
    assert data["verdict"] == "FAIL"
    assert [check["status"] for check in data["checks"]] == ["FAIL"] + ["NOT TESTED"] * 3


class Unpluggable(PyserialPort):
    def __init__(self, port: serial.Serial, cable: dict[str, bool]) -> None:
        super().__init__(port)
        self.cable = cable

    def read(self, size: int) -> bytes:
        if not self.cable["plugged"]:
            raise serial.SerialException("device disconnected")
        return super().read(size)


def test_interactive_replug_detects_disconnect_backoff_and_reconnect(emulator):
    emu, path = emulator
    cable = {"plugged": True}

    def opener(device: str) -> Unpluggable:
        if not cable["plugged"]:
            raise serial.SerialException("no such device")
        return Unpluggable(serial.Serial(device, 115200, timeout=0, write_timeout=0), cable)

    def human(message: str) -> None:
        if message.startswith("UNPLUG"):
            cable["plugged"] = False
        elif message.startswith("PLUG"):
            emu.reboot(ResetCause.POWER_ON)
            cable["plugged"] = True

    bench = make_bench(path, emu, FAST, interactive=True, transport_opener=opener,
                       present=lambda device: cable["plugged"], prompt=human)
    results = {check.id: check for check in check_replug(bench)}
    assert results["usb.disconnect"].status is Status.PASS
    assert results["usb.reconnect"].status is Status.PASS
    backoff = results["usb.backoff"]
    assert backoff.status is Status.PASS, backoff.observed
    assert backoff.details["expected_s"][:3] == [0.2, 0.4, 0.8]


def fake_port(device: str, vid: int | None, pid: int | None, serial_number: str | None) -> \
        ListPortInfo:
    port = ListPortInfo(device, skip_link_detection=True)
    port.vid, port.pid, port.serial_number = vid, pid, serial_number
    port.description = "USB serial"
    return port


def test_discovery_filters_usb_recognizes_uno_and_redacts_serials(tmp_path):
    serial_number = "75834353130351D0C1A1"
    link = tmp_path / f"usb-Arduino__www.arduino.cc__0043_{serial_number}-if00"
    link.symlink_to("/dev/ttyACM0")
    ports = [fake_port("/dev/ttyACM0", 0x2341, 0x0043, serial_number),
             fake_port("/dev/ttyS0", None, None, None),
             fake_port("/dev/ttyUSB0", 0x10C4, 0xEA60, "0001")]
    found = candidates(ports, tmp_path)
    assert [c.device for c in found] == ["/dev/ttyACM0", "/dev/ttyUSB0"]
    uno = found[0]
    assert (uno.usb_id, uno.recognized, uno.description) == ("2341:0043", True, "Arduino Uno R3")
    assert serial_number not in str(uno) and uno.serial == "…C1A1"
    assert uno.by_id is not None and uno.by_id.endswith("…C1A1-if00")
    assert describe(str(link), ports, tmp_path) == uno
    assert describe("/dev/ttyACM7", ports, tmp_path) is None


def test_upload_check_reads_the_flash_record(tmp_path):
    record = {"variant": "production", "hex": "production.hex", "sha256": "ab" * 32,
              "avrdude_exit_code": 0, "utc": "20261106T120000Z"}
    path = tmp_path / "last-flash.json"
    path.write_text(json.dumps(record))
    assert checks.check_upload(cli.read_flash_record(path)).status is Status.PASS
    path.write_text(json.dumps(record | {"variant": "watchdog-test"}))
    assert checks.check_upload(cli.read_flash_record(path)).status is Status.FAIL
    assert checks.check_upload(cli.read_flash_record(tmp_path / "none.json")).status is \
        Status.NOT_TESTED


def test_cli_run_writes_reports_and_logs(emulator, tmp_path, monkeypatch):
    emu, path = emulator
    monkeypatch.setattr(cli, "HARDWARE", replace(FAST, sustained_s=0.3))
    monkeypatch.setattr(cli, "dtr_reset", lambda port, sleep: emu.reboot(ResetCause.EXTERNAL))
    assert cli.main(["run", "--device", path, "--report-dir", str(tmp_path)]) == cli.EXIT_OK
    (run_dir,) = tmp_path.iterdir()
    data = json.loads((run_dir / "report.json").read_text())
    assert data["verdict"] == "PASS" and data["target"]["physical"] is False
    assert "SIMULATED TARGET" in (run_dir / "report.md").read_text()
    assert "PASS gg1.exchange" in (run_dir / "run.log").read_text()


def test_firmware_banner_matches_the_harness_constant():
    source = (ROOT / "firmware" / "arduino-uno" / "src" / "main.cpp").read_text()
    assert WATCHDOG_TEST_BANNER.decode() + "\\n" in source
    assert source.count("#ifdef GUARDIAN_WATCHDOG_TEST") == 3


def test_makefile_keeps_the_watchdog_test_interlocks():
    makefile = (ROOT / "bench" / "Makefile").read_text()
    assert '[ "$(CONFIRM)" = watchdog-test ]' in makefile
    assert "glucose_guardian.bench authorize-watchdog" in makefile
    assert "sha256sum --check" in makefile
