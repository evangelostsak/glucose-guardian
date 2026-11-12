import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from harness import ROOT

from glucose_guardian.bench import authorization, checks, flash_record
from glucose_guardian.bench.authorization import (
    AVRDUDE_BASE,
    MAX_AGE_S,
    AuthorizationError,
    Host,
    authorize,
    check_bootloader,
)
from glucose_guardian.bench.flash_record import verify_watchdog_flash, write_flash_record
from glucose_guardian.bench.report import Status

SERIAL = "75834353130351D0C1A1"
OPTIBOOT_4_4 = bytes(32766) + bytes([4, 4])
TOOLING: dict[str, str | None] = {"harness": "0.1.0", "avrdude": "7.1"}


class Bench:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.now = 1000.0
        for directory in ("sys/class/tty", "sys/bus/usb/devices", "sys/devices/usb1",
                          "by-id", "dev", "out"):
            (root / directory).mkdir(parents=True)
        (root / "boot_id").write_text("boot-a\n")
        self.out = root / "out"
        self.build(b":00000001FF\n")
        (root / "out" / "BUILD-COMMIT").write_text("45a33bb\n")
        self.host = Host(by_id_dir=root / "by-id", sysfs=root / "sys",
                         boot_id=root / "boot_id", clock=lambda: self.now)
        self.record = root / "reports" / "bootloader-authorization.json"
        self.flash = root / "reports" / "last-flash.json"
        self.calls: list[list[str]] = []
        hub = self.attach_dir("usb1", "1d6b", "0002", None, 1)
        (root / "sys" / "bus" / "usb" / "devices" / "usb1").symlink_to(hub)

    def build(self, watchdog_image: bytes) -> None:
        images = {"production.hex": b":10000000PRODUCTION\n",
                  "watchdog-test-DO-NOT-DEPLOY.hex": watchdog_image}
        for name, content in images.items():
            (self.out / name).write_bytes(content)
        (self.out / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in images.items()))

    def attach_dir(self, port: str, vid: str, pid: str, serial: str | None,
                   devnum: int) -> Path:
        usb = self.root / "sys" / "devices" / "usb1" / port
        usb.mkdir(exist_ok=True)
        for name, value in (("idVendor", vid), ("idProduct", pid), ("devnum", str(devnum))):
            (usb / name).write_text(value + "\n")
        if serial is not None:
            (usb / "serial").write_text(serial + "\n")
        return usb

    def attach(self, port: str, tty: str, vid: str, pid: str, serial: str | None,
               link: str, usb_serial_driver: bool) -> str:
        usb = self.attach_dir(port, vid, pid, serial, 5)
        interface = usb / f"{port}:1.0"
        node = interface / tty if usb_serial_driver else interface
        node.mkdir(parents=True)
        devices = self.root / "sys" / "bus" / "usb" / "devices"
        (devices / port).symlink_to(usb)
        (devices / interface.name).symlink_to(interface)
        (self.root / "sys" / "class" / "tty" / tty).mkdir()
        (self.root / "sys" / "class" / "tty" / tty / "device").symlink_to(node)
        (self.root / "dev" / tty).touch()
        by_id = self.root / "by-id" / link
        by_id.symlink_to(self.root / "dev" / tty)
        return str(by_id)

    def uno(self) -> str:
        return self.attach("1-1.3", "ttyACM0", "2341", "0043", SERIAL,
                           f"usb-Arduino__www.arduino.cc__0043_{SERIAL}-if00", False)

    def clone(self, port: str, tty: str) -> str:
        return self.attach(port, tty, "1a86", "7523", None, "usb-1a86_USB2.0-Serial-if00-port0",
                           True)

    def replug(self, port: str) -> None:
        devnum = self.root / "sys" / "devices" / "usb1" / port / "devnum"
        devnum.write_text(str(int(devnum.read_text()) + 1) + "\n")

    def reader(self, dump: bytes, code: int,
               during: Callable[[], None] | None) -> Callable[[list[str]], int]:
        def read(argv: list[str]) -> int:
            self.calls.append(argv)
            Path(argv[-1].split(":")[2]).write_bytes(dump)
            if during is not None:
                during()
            return code

        return read

    def check(self, device: str) -> dict[str, object]:
        return check_bootloader(device, self.record, self.out, self.host,
                                self.reader(OPTIBOOT_4_4, 0, None), TOOLING,
                                "2026-11-07T10:00:00Z")

    def authorize(self, device: str) -> dict[str, object]:
        return authorize(device, self.record, self.out, self.host)

    def rewrite(self, change: Callable[[dict[str, object]], None], resign: bool) -> None:
        rewrite(self.record, change, resign)

    def flashed(self, device: str, variant: str, image: str, code: int) -> dict[str, object]:
        return write_flash_record(device, variant, self.out / image, self.out, code,
                                  "20261107T100500Z", self.flash, self.host)

    def verify_flash(self, device: str) -> dict[str, object]:
        return verify_watchdog_flash(device, self.flash, self.out, self.host)


def rewrite(path: Path, change: Callable[[dict[str, object]], None], resign: bool) -> None:
    data = json.loads(path.read_text())
    change(data)
    if resign:
        body = {key: value for key, value in data.items() if key != "integrity_sha256"}
        data["integrity_sha256"] = authorization.digest(body)
    path.write_text(json.dumps(data))


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


def test_valid_authorization_is_bound_to_device_boot_and_build(bench):
    device = bench.uno()
    bench.check(device)
    text = bench.record.read_text()
    data = json.loads(text)
    assert SERIAL not in text
    assert data["device"]["by_id"].endswith("…C1A1-if00")
    assert data["device"]["usb_id"] == "2341:0043"
    assert (data["device"]["usb_port"], data["device"]["devnum"]) == ("1-1.3", 5)
    assert data["bootloader"]["classification"] == "optiboot"
    assert data["bootloader"]["version"] == "4.4"
    assert data["host_boot_id"] == "boot-a" and data["build"]["commit"] == "45a33bb"
    assert data["tooling"] == TOOLING and data["verified_utc"] == "2026-11-07T10:00:00Z"
    bench.now += MAX_AGE_S
    assert bench.authorize(device) == data


def test_bootloader_check_only_reads_flash_and_removes_the_dump(bench):
    device = bench.uno()
    bench.check(device)
    dump = bench.record.with_name("flash-dump.bin")
    assert bench.calls == [["avrdude", "-p", "atmega328p", "-c", "arduino", "-b", "115200",
                            "-P", device, "-U", f"flash:r:{dump}:r"]]
    assert not dump.exists()


def test_makefile_programmer_arguments_match_the_read_command():
    makefile = (ROOT / "bench" / "Makefile").read_text()
    match = re.search(r"^AVRDUDE = (.*)$", makefile, re.MULTILINE)
    assert match is not None
    assert match.group(1).split() == [*AVRDUDE_BASE, "-P", "$(DEVICE)"]


@pytest.mark.parametrize("dump,code,reason", [
    (bytes(32766) + bytes([2, 3]), 0, "older than Optiboot 4"),
    (bytes(32766) + bytes([0xFF, 0xFF]), 0, "unknown"),
    (bytes(30000), 0, "no Optiboot version marker"),
    (OPTIBOOT_4_4, 1, "avrdude exited with code 1"),
])
def test_failed_bootloader_check_removes_any_previous_authorization(bench, dump, code, reason):
    device = bench.uno()
    bench.check(device)
    with pytest.raises(AuthorizationError, match=reason):
        check_bootloader(device, bench.record, bench.out, bench.host,
                         bench.reader(dump, code, None), TOOLING, "2026-11-07T10:05:00Z")
    assert not bench.record.exists()
    assert not bench.record.with_name("flash-dump.bin").exists()
    with pytest.raises(AuthorizationError, match="no bootloader authorization"):
        bench.authorize(device)


def test_connection_change_during_the_read_writes_no_record(bench):
    device = bench.uno()
    with pytest.raises(AuthorizationError, match="changed during the read"):
        check_bootloader(device, bench.record, bench.out, bench.host,
                         bench.reader(OPTIBOOT_4_4, 0, lambda: bench.replug("1-1.3")), TOOLING,
                         "2026-11-07T10:00:00Z")
    assert not bench.record.exists()


def test_authorization_for_another_board_is_rejected(bench):
    checked = bench.uno()
    other = bench.attach("1-1.2", "ttyACM1", "2341", "0043", "95536333535351E0B0C2",
                         "usb-Arduino__www.arduino.cc__0043_95536333535351E0B0C2-if00", False)
    bench.check(checked)
    with pytest.raises(AuthorizationError, match="another board") as error:
        bench.authorize(other)
    assert {"by_id", "usb_port", "serial_sha256"} <= set(
        re.search(r"\((.*) differ\)", str(error.value)).group(1).split(", "))


def test_the_same_link_now_naming_another_board_is_rejected(bench):
    device = bench.uno()
    bench.check(device)
    usb = bench.root / "sys" / "devices" / "usb1" / "1-1.3"
    (usb / "serial").write_text("85536333535351E0B0C3\n")
    with pytest.raises(AuthorizationError, match="another board.*serial_sha256"):
        bench.authorize(device)


@pytest.mark.parametrize("serial", [SERIAL, None])
def test_reconnecting_requires_a_new_check(bench, serial):
    device = bench.uno() if serial else bench.clone("1-1.3", "ttyUSB0")
    data = bench.check(device)
    assert (data["device"]["serial_sha256"] is None) == (serial is None)
    bench.authorize(device)
    bench.replug("1-1.3")
    with pytest.raises(AuthorizationError, match=r"\(devnum differ\)"):
        bench.authorize(device)


def reboot(bench: Bench) -> None:
    (bench.root / "boot_id").write_text("boot-b\n")


def rebuild(bench: Bench) -> None:
    (bench.out / "SHA256SUMS").write_text("cd  watchdog-test-DO-NOT-DEPLOY.hex\n")


def recommit(bench: Bench) -> None:
    (bench.out / "BUILD-COMMIT").write_text("51e0b0c\n")


def expire(bench: Bench) -> None:
    bench.now += MAX_AGE_S + 1


def rewind(bench: Bench) -> None:
    bench.now -= 1


@pytest.mark.parametrize("change,reason", [
    (expire, "age 901 s is outside"),
    (rewind, "age -1 s is outside"),
    (reboot, "earlier boot of this host"),
    (rebuild, "bench build changed"),
    (recommit, "bench build changed"),
])
def test_stale_authorization_is_rejected(bench, change, reason):
    device = bench.uno()
    bench.check(device)
    change(bench)
    with pytest.raises(AuthorizationError, match=reason):
        bench.authorize(device)


@pytest.mark.parametrize("change", [
    lambda data: data["device"].update(usb_port="1-1.2"),
    lambda data: data["bootloader"].update(version="8.3"),
    lambda data: data.update(verified_boottime_s=data["verified_boottime_s"] + 600),
    lambda data: data.update(host_boot_id="boot-b"),
    lambda data: data["build"].update(commit="51e0b0c"),
    lambda data: data.update(integrity_sha256="0" * 64),
])
def test_modified_authorization_is_rejected(bench, change):
    device = bench.uno()
    bench.check(device)
    bench.rewrite(change, resign=False)
    with pytest.raises(AuthorizationError, match="modified after it was written"):
        bench.authorize(device)


@pytest.mark.parametrize("change,reason", [
    (lambda data: data["bootloader"].update(marker="0203", version="3.2"), "older than"),
    (lambda data: data["bootloader"].update(classification="atmegaboot"), "supported bootloader"),
    (lambda data: data["bootloader"].update(marker="zz"), "bootloader marker"),
    (lambda data: data["bootloader"].pop("region_sha256"), "malformed .*bootloader"),
    (lambda data: data.update(schema="glucose-guardian/bootloader-authorization/2"),
     "unsupported authorization schema"),
    (lambda data: data.update(verified_boottime_s="1000.0"), "verified_boottime_s"),
    (lambda data: data.update(device=[]), "malformed .*device"),
    (lambda data: data["build"].update(extra="x"), "malformed .*build"),
])
def test_resigned_but_invalid_authorization_is_rejected(bench, change, reason):
    device = bench.uno()
    bench.check(device)
    bench.rewrite(change, resign=True)
    with pytest.raises(AuthorizationError, match=reason):
        bench.authorize(device)


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"schema": 1}', "\udcff"])
def test_malformed_authorization_is_rejected(bench, content):
    device = bench.uno()
    bench.record.parent.mkdir()
    bench.record.write_bytes(content.encode(errors="surrogateescape"))
    with pytest.raises(AuthorizationError, match="unreadable|malformed"):
        bench.authorize(device)


def test_missing_authorization_is_rejected(bench):
    with pytest.raises(AuthorizationError, match="no bootloader authorization"):
        bench.authorize(bench.uno())


def not_by_id(bench: Bench) -> str:
    bench.uno()
    return str(bench.root / "dev" / "ttyACM0")


def dangling(bench: Bench) -> str:
    device = bench.uno()
    (bench.root / "dev" / "ttyACM0").unlink()
    return device


def not_usb(bench: Bench) -> str:
    platform = bench.root / "sys" / "devices" / "platform" / "serial8250"
    platform.mkdir(parents=True)
    (bench.root / "sys" / "class" / "tty" / "ttyS0").mkdir()
    (bench.root / "sys" / "class" / "tty" / "ttyS0" / "device").symlink_to(platform)
    (bench.root / "dev" / "ttyS0").touch()
    link = bench.root / "by-id" / "pci-serial"
    link.symlink_to(bench.root / "dev" / "ttyS0")
    return str(link)


def twin_clones(bench: Bench) -> str:
    bench.attach_dir("1-1.2", "1a86", "7523", None, 7)
    (bench.root / "sys" / "bus" / "usb" / "devices" / "1-1.2").symlink_to(
        bench.root / "sys" / "devices" / "usb1" / "1-1.2")
    return bench.clone("1-1.3", "ttyUSB0")


def twin_serials(bench: Bench) -> str:
    bench.attach_dir("1-1.2", "2341", "0043", SERIAL, 7)
    (bench.root / "sys" / "bus" / "usb" / "devices" / "1-1.2").symlink_to(
        bench.root / "sys" / "devices" / "usb1" / "1-1.2")
    return bench.uno()


@pytest.mark.parametrize("select,reason", [
    (not_by_id, "must be a link in"),
    (dangling, "does not point to an attached device"),
    (not_usb, "is not a USB device"),
    (twin_clones, "2 attached USB devices share"),
    (twin_serials, "2 attached USB devices share"),
])
def test_unverifiable_identity_is_rejected(bench, select, reason):
    device = select(bench)
    with pytest.raises(AuthorizationError, match=reason):
        bench.check(device)
    assert bench.calls == [] and not bench.record.exists()
    with pytest.raises(AuthorizationError, match="no bootloader authorization"):
        bench.authorize(device)


def test_identity_lost_after_the_check_is_rejected(bench):
    device = bench.uno()
    bench.check(device)
    (bench.root / "dev" / "ttyACM0").unlink()
    with pytest.raises(AuthorizationError, match="does not point to an attached device"):
        bench.authorize(device)


def make(tmp_path: Path, avrdude_exit: int, *arguments: str) -> subprocess.CompletedProcess[str]:
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    fake = tools / "avrdude"
    fake.write_text(f'#!/bin/sh\ncase "$*" in *-U*) echo "$@" >> {tmp_path}/avrdude.log;; esac\n'
                    f"exit {avrdude_exit}\n")
    fake.chmod(0o755)
    return subprocess.run(
        ["make", "-s", "-C", str(ROOT / "bench"), f"PYTHON={sys.executable}",
         f"REPORTS={tmp_path / 'reports'}", f"OUT={tmp_path / 'out'}",
         "DEVICE=/dev/serial/by-id/usb-not-attached", *arguments],
        env=os.environ | {"PATH": f"{tools}:{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=60, check=False)


def test_makefile_never_runs_avrdude_without_confirmation_and_authorization(tmp_path):
    unconfirmed = make(tmp_path, 0, "flash", "VARIANT=watchdog-test")
    assert unconfirmed.returncode != 0 and "needs CONFIRM=watchdog-test" in unconfirmed.stderr
    refused = make(tmp_path, 0, "flash", "VARIANT=watchdog-test", "CONFIRM=watchdog-test")
    assert refused.returncode != 0
    assert "REFUSED: no bootloader authorization" in refused.stderr
    check = make(tmp_path, 0, "bootloader")
    assert check.returncode != 0 and "Bootloader check FAILED" in check.stderr
    assert not (tmp_path / "avrdude.log").exists()


WATCHDOG_HEX = "watchdog-test-DO-NOT-DEPLOY.hex"


def test_verified_watchdog_flash_record_is_accepted(bench):
    device = bench.uno()
    bench.flashed(device, "watchdog-test", WATCHDOG_HEX, 0)
    text = bench.flash.read_text()
    data = json.loads(text)
    assert SERIAL not in text
    assert (data["variant"], data["hex"], data["avrdude_exit_code"]) == \
        ("watchdog-test", WATCHDOG_HEX, 0)
    assert data["sha256"] == hashlib.sha256((bench.out / WATCHDOG_HEX).read_bytes()).hexdigest()
    assert (data["device"]["usb_port"], data["device"]["devnum"]) == ("1-1.3", 5)
    bench.now += flash_record.MAX_AGE_S
    assert bench.verify_flash(device) == data


def test_flash_record_still_feeds_the_upload_check(bench):
    device = bench.uno()
    record = bench.flashed(device, "production", "production.hex", 0)
    assert checks.check_upload(record).status is Status.PASS


@pytest.mark.parametrize("variant,image,code,reason", [
    ("production", "production.hex", 0, "not the watchdog-test image"),
    ("watchdog-test", WATCHDOG_HEX, 1, r"did not succeed \(avrdude exit 1\)"),
])
def test_recorded_production_or_failed_flash_is_refused(bench, variant, image, code, reason):
    device = bench.uno()
    bench.flashed(device, variant, image, code)
    with pytest.raises(AuthorizationError, match=reason):
        bench.verify_flash(device)


@pytest.mark.parametrize("variant,image", [
    ("watchdog-test", "production.hex"),
    ("production", WATCHDOG_HEX),
    ("bootloader", WATCHDOG_HEX),
])
def test_flash_record_writer_refuses_an_image_of_another_variant(bench, variant, image):
    device = bench.uno()
    with pytest.raises(AuthorizationError, match="is not the"):
        bench.flashed(device, variant, image, 0)
    assert not bench.flash.exists()


def tamper_image(bench: Bench) -> None:
    (bench.out / WATCHDOG_HEX).write_bytes(b":00000001FE\n")


def drop_sums(bench: Bench) -> None:
    (bench.out / "SHA256SUMS").unlink()


def garble_sums(bench: Bench) -> None:
    (bench.out / "SHA256SUMS").write_text("not a checksum line\n")


def unlist_image(bench: Bench) -> None:
    sums = bench.out / "SHA256SUMS"
    sums.write_text(sums.read_text().splitlines()[0] + "\n")


@pytest.mark.parametrize("change,reason", [
    (expire, "flash record age 901 s is outside"),
    (rewind, "flash record age -1 s is outside"),
    (reboot, "flash record is from an earlier boot"),
    (lambda bench: bench.replug("1-1.3"), r"another board or connection \(devnum differ\)"),
    (lambda bench: bench.build(b":00000001FD\n"), "differs from this build's watchdog-test"),
    (tamper_image, "does not match SHA256SUMS"),
    (drop_sums, "cannot read"),
    (garble_sums, "malformed"),
    (unlist_image, "does not list"),
    (recommit, "bench build changed since the flash record"),
])
def test_stale_or_mismatched_flash_evidence_is_refused(bench, change, reason):
    device = bench.uno()
    bench.flashed(device, "watchdog-test", WATCHDOG_HEX, 0)
    change(bench)
    with pytest.raises(AuthorizationError, match=reason):
        bench.verify_flash(device)


def test_flash_record_for_another_board_is_refused(bench):
    flashed = bench.uno()
    other = bench.attach("1-1.2", "ttyACM1", "2341", "0043", "95536333535351E0B0C2",
                         "usb-Arduino__www.arduino.cc__0043_95536333535351E0B0C2-if00", False)
    bench.flashed(flashed, "watchdog-test", WATCHDOG_HEX, 0)
    with pytest.raises(AuthorizationError, match="flash record is for another board"):
        bench.verify_flash(other)


@pytest.mark.parametrize("change", [
    lambda data: data.update(variant="production"),
    lambda data: data.update(sha256="0" * 64),
    lambda data: data.update(avrdude_exit_code=1),
    lambda data: data.update(hex="production.hex"),
    lambda data: data.update(flashed_boottime_s=data["flashed_boottime_s"] + 600),
    lambda data: data["device"].update(devnum=6),
])
def test_modified_flash_record_is_refused(bench, change):
    device = bench.uno()
    bench.flashed(device, "watchdog-test", WATCHDOG_HEX, 0)
    rewrite(bench.flash, change, resign=False)
    with pytest.raises(AuthorizationError, match="flash record was modified after it was written"):
        bench.verify_flash(device)


@pytest.mark.parametrize("change,reason", [
    (lambda data: data.update(variant="production"), "not the watchdog-test image"),
    (lambda data: data.update(hex="production.hex"), "not the watchdog-test image"),
    (lambda data: data.update(avrdude_exit_code=1), "did not succeed"),
    (lambda data: data.update(avrdude_exit_code=True), "did not succeed"),
    (lambda data: data.update(avrdude_exit_code=False), "did not succeed"),
    (lambda data: data.update(avrdude_exit_code=0.0), "did not succeed"),
    (lambda data: data.update(avrdude_exit_code="0"), "did not succeed"),
    (lambda data: data.update(avrdude_exit_code=None), "did not succeed"),
    (lambda data: data.update(sha256="0" * 64), "differs from this build's"),
    (lambda data: data.update(schema="glucose-guardian/flash-record/2"),
     "unsupported flash schema"),
    (lambda data: data.update(flashed_boottime_s="1000.0"), "flashed_boottime_s"),
    (lambda data: data.update(device={}), "malformed flash record: device"),
    (lambda data: data["build"].update(extra="x"), "malformed flash record: build"),
])
def test_resigned_but_invalid_flash_record_is_refused(bench, change, reason):
    device = bench.uno()
    bench.flashed(device, "watchdog-test", WATCHDOG_HEX, 0)
    rewrite(bench.flash, change, resign=True)
    with pytest.raises(AuthorizationError, match=reason):
        bench.verify_flash(device)


LEGACY_RECORD = json.dumps({"variant": "watchdog-test", "hex": WATCHDOG_HEX, "sha256": "ab" * 32,
                            "avrdude_exit_code": 0, "utc": "20261107T100500Z"})


@pytest.mark.parametrize("content", ["", "not json", "[]", "\udcff", LEGACY_RECORD])
def test_malformed_or_legacy_flash_record_is_refused(bench, content):
    device = bench.uno()
    bench.flash.parent.mkdir()
    bench.flash.write_bytes(content.encode(errors="surrogateescape"))
    with pytest.raises(AuthorizationError, match="unreadable flash record|malformed flash record"):
        bench.verify_flash(device)


def test_missing_flash_record_is_refused(bench):
    with pytest.raises(AuthorizationError, match="no flash record"):
        bench.verify_flash(bench.uno())


def test_flash_record_without_identity_is_refused(bench):
    device = bench.uno()
    bench.flashed(device, "watchdog-test", WATCHDOG_HEX, 0)
    (bench.root / "dev" / "ttyACM0").unlink()
    with pytest.raises(AuthorizationError, match="does not point to an attached device"):
        bench.verify_flash(device)


def test_makefile_watchdog_refuses_without_a_verified_flash_record(tmp_path):
    result = make(tmp_path, 0, "watchdog")
    assert result.returncode != 0 and "Verdict: FAIL" in result.stdout
    (report,) = (tmp_path / "reports").glob("watchdog-*/report.json")
    found = {check["id"]: check for check in json.loads(report.read_text())["checks"]}
    assert found["watchdog.flash_record"]["status"] == "FAIL"
    record = tmp_path / "reports" / "last-flash.json"
    assert f"no flash record at {record};" in found["watchdog.flash_record"]["observed"]
    assert {found[key]["status"] for key in
            ("watchdog.identity", "watchdog.reset", "watchdog.cause")} == {"NOT TESTED"}


def test_makefile_flash_needs_a_by_id_device(tmp_path):
    result = make(tmp_path, 0, "flash", "VARIANT=production", "DEVICE=/dev/ttyACM0")
    assert result.returncode != 0 and "must be a /dev/serial/by-id link" in result.stderr
    assert not (tmp_path / "avrdude.log").exists()


def test_makefile_flash_discards_the_previous_record_before_writing(tmp_path):
    Bench(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "last-flash.json").write_text(LEGACY_RECORD)
    result = make(tmp_path, 1, "flash", "VARIANT=production")
    assert result.returncode != 0 and "Flash record NOT written" in result.stderr
    assert (tmp_path / "avrdude.log").read_text().count("flash:w:") == 1
    assert not (reports / "last-flash.json").exists()
