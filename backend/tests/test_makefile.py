import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from harness import ROOT

pytestmark = pytest.mark.skipif(shutil.which("make") is None or sys.platform != "linux",
                                reason="the Makefile targets Linux hosts with GNU make")

MAKEFILE = ROOT / "Makefile"
FAKE_VENV = "/fake/venv"
PORT = "/dev/serial/by-id/usb-Arduino_Uno_SYNTHETIC-if00"
SECRET = "synthetic-secret-typed-at-the-prompt"
PI = ["PI_USER=pi", "PI_HOST=raspberrypi.local"]
EVERY_VARIABLE = [f"VENV={FAKE_VENV}", f"SERIAL_PORT={PORT}", "VARIANT=production", *PI]
DEXCOM_CONTACT = ("sudo systemd-run", "glucose_guardian.share_check")
FIRMWARE_WRITE = ("flash:w:", " flash DEVICE=")


def clean_environment(extra: dict[str, str]) -> dict[str, str]:
    kept = {key: value for key, value in os.environ.items()
            if not key.startswith("MAKE") and key not in {"MFLAGS", "CREDENTIALS_DIRECTORY"}}
    return kept | extra


def make(*args: str, cwd: Path = ROOT, extra: dict[str, str] | None = None,
         root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["make", "-C", str(root), *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL,
                          env=clean_environment(extra or {}))


def dry(*args: str) -> str:
    result = make("-n", *args)
    assert result.returncode == 0, result.stderr
    return result.stdout


def phony_targets() -> list[str]:
    text = MAKEFILE.read_text()
    block = re.search(r"^\.PHONY:(.*?)(?=\n\S)", text, re.S | re.M)
    assert block is not None
    return block.group(1).replace("\\", " ").split()


def test_help_is_the_default_target_from_any_directory(tmp_path):
    default = make(cwd=tmp_path)
    assert default.returncode == 0
    assert default.stdout == make("help").stdout
    for section in ("Development [laptop]", "Firmware", "Bench [Pi]", "Simulation [laptop]",
                    "Raspberry Pi", "Dexcom Share [Pi]", "Maintenance", "Variables"):
        assert section in default.stdout


def test_every_phony_target_is_described_in_help():
    described = set(re.findall(r"^  ([a-z][a-z0-9-]*) {2,}\S", make("help").stdout, re.M))
    assert described == set(phony_targets())


@pytest.mark.parametrize("target,args,expected", [
    ("setup", [], [f"{FAKE_VENV}/bin/python -m pip install -e '{ROOT}/backend[test,lint]'",
                   f"python3 -m venv {FAKE_VENV}"]),
    ("setup-firmware", [], [f"{FAKE_VENV}/bin/python -m pip install 'platformio==6.1.18'"]),
    ("lint", [], [f"cd {ROOT}/backend && {FAKE_VENV}/bin/python -m ruff check src tests"]),
    ("typecheck", [], [f"cd {ROOT}/backend && {FAKE_VENV}/bin/python -m mypy src"]),
    ("test", [], [f"cd {ROOT}/backend && {FAKE_VENV}/bin/python -m pytest"]),
    ("firmware-build", [], [f'PATH="{FAKE_VENV}/bin:$PATH" make -C {ROOT}/bench build',
                            f"pio run -d {ROOT}/firmware/arduino-uno -e uno -e uno-watchdog-test",
                            "out/watchdog-test-DO-NOT-DEPLOY.hex", "SHA256SUMS"]),
    ("firmware-test", [], [f"pio test --project-dir {ROOT}/firmware/arduino-uno -e native"]),
    ("check", [], ["-m ruff check src tests", "-m mypy src", "-m pytest",
                   "-e uno -e uno-watchdog-test", "-e native"]),
    ("simulator", [], ["-m glucose_guardian.share_simulator --port 8765 --scenario normal"]),
    ("simulate", [], [f"env -u CREDENTIALS_DIRECTORY {FAKE_VENV}/bin/python -m "
                      f"glucose_guardian.service --config {ROOT}/config/example.toml"]),
    ("bench-setup", [], ["sudo apt-get install -y python3-venv avrdude usbutils",
                         f"python3 -m venv {FAKE_VENV}"]),
    ("bench-discover", [], [f"{FAKE_VENV}/bin/python -m glucose_guardian.bench discover"]),
    ("bench-bootloader", [f"SERIAL_PORT={PORT}"], [f"-m glucose_guardian.bench bootloader "
                                                  f"--device {PORT}"]),
    ("bench-run", [f"SERIAL_PORT={PORT}"], [f"-m glucose_guardian.bench run --device {PORT}"]),
    ("bench-run-interactive", [f"SERIAL_PORT={PORT}"], ["--interactive"]),
    ("bench-watchdog", [f"SERIAL_PORT={PORT}"], [f"-m glucose_guardian.bench watchdog "
                                                f"--device {PORT}"]),
    ("bench-collect", [], ["tar -czf bench-diagnostics-"]),
    ("firmware-flash", [f"SERIAL_PORT={PORT}", "VARIANT=production"],
     ['Type "flash production" to continue:',
      f"make -C {ROOT}/bench VENV={FAKE_VENV} PYTHON={FAKE_VENV}/bin/python flash "
      f"DEVICE={PORT} VARIANT=production", "flash:w:"]),
    ("pi-sync", PI, ["ssh pi@raspberrypi.local mkdir -p glucose-guardian",
                     "rsync -a --delete", "--exclude 'config/local.*'",
                     "--exclude bench/reports", f"{ROOT}/bench {ROOT}/config",
                     "pi@raspberrypi.local:glucose-guardian/"]),
    ("pi-setup", PI, ["ssh -t pi@raspberrypi.local 'cd glucose-guardian && make bench-setup'"]),
    ("pi-status", PI, ["ssh pi@raspberrypi.local 'cd glucose-guardian && make status'"]),
    ("dexcom-config", ["STATE_DIR=/state"], ["install -d -m 0700 /state",
                                              "sed 's|/var/lib/glucose-guardian|/state|'"]),
    ("dexcom-credentials", [], ["systemd-ask-password -n \"Dexcom Share $name:\" | sudo "
                                "systemd-creds encrypt --name=dexcom-$name - \"$file.new\""]),
    ("dexcom-status", ["STATE_DIR=/state"], ["-m glucose_guardian.share_state --state-file "
                                              "/state/dexcom-share-state.json status"]),
    ("dexcom-check", [], ['Type "contact dexcom" to send these requests:',
                          "sudo systemd-run --pipe --wait",
                          "LoadCredentialEncrypted=dexcom-username:/etc/glucose-guardian/"
                          "credentials/dexcom-username.cred",
                          "-m glucose_guardian.share_check --config"]),
    ("clean", [], ["rm -rf", "-name __pycache__"]),
])
def test_targets_run_the_intended_commands(target, args, expected):
    output = dry(target, f"VENV={FAKE_VENV}", *args)
    for command in expected:
        assert command in output


def test_only_dexcom_check_can_contact_dexcom_and_only_firmware_flash_writes_firmware():
    for target in phony_targets():
        output = dry(target, *EVERY_VARIABLE)
        contacts = any(marker in output for marker in DEXCOM_CONTACT)
        writes = any(marker in output for marker in FIRMWARE_WRITE)
        assert contacts == (target == "dexcom-check"), target
        assert writes == (target == "firmware-flash"), target


def test_clean_names_only_caches_and_build_output():
    output = dry("clean")
    removed = re.findall(r"(?:rm -rf|find) (.*)", output)
    text = " ".join(removed)
    for kept in (".venv", "bench/out", "bench/reports", "config", "glucose-guardian-state"):
        assert kept not in text
    assert all(path.startswith(str(ROOT)) for path in re.findall(r"/\S+", text))


@pytest.mark.parametrize("args,message", [
    (["firmware-flash"], "SERIAL_PORT is required"),
    (["firmware-flash", f"SERIAL_PORT={PORT}"], "VARIANT is required"),
    (["bench-run"], "SERIAL_PORT is required"),
    (["bench-watchdog"], "SERIAL_PORT is required"),
    (["bench-bootloader"], "SERIAL_PORT is required"),
    (["pi-sync"], "PI_USER is required"),
    (["pi-sync", "PI_USER=pi"], "PI_HOST is required"),
    (["pi-status", "PI_HOST=raspberrypi.local"], "PI_USER is required"),
])
def test_missing_variables_stop_before_any_command(args, message):
    result = make("-n", *args)
    assert result.returncode != 0 and message in result.stderr
    assert result.stdout == ""


def fake_tools(directory: Path) -> Path:
    bin_dir = directory / "fakebin"
    bin_dir.mkdir()
    scripts = {
        "sudo": '[ "$1" = "-v" ] && exit 0\nexec "$@"',
        "systemd-run": "exit 1",
        "systemd-ask-password": f"printf '%s' '{SECRET}'",
        "systemd-creds": 'env >> "$FAKE_ENVIRONMENTS"\ncat > /dev/null\n'
                         'for last in "$@"; do :; done\necho encrypted > "$last"',
        "ssh": "exit 0",
        "rsync": "exit 0",
        "avrdude": "exit 0",
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(f'#!/bin/bash\nprintf "%s\\n" "{name} $*" >> "$FAKE_LOG"\n{body}\n')
        path.chmod(0o755)
    return bin_dir


def fake_venv(directory: Path) -> Path:
    python = directory / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}/backend/src" exec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    return python.parent.parent


@pytest.fixture
def host(tmp_path):
    log = tmp_path / "calls.log"
    log.touch()
    environments = tmp_path / "environments.log"
    environments.touch()
    bin_dir = fake_tools(tmp_path)
    config = tmp_path / "local.dexcom.toml"
    config.write_text("synthetic\n")
    credentials = tmp_path / "credentials"
    return {
        "log": log, "environments": environments, "credentials": credentials,
        "env": {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
                "FAKE_ENVIRONMENTS": str(environments)},
        "args": [f"VENV={fake_venv(tmp_path)}", f"DEXCOM_CONFIG={config}",
                 f"STATE_DIR={tmp_path / 'state'}", f"CREDENTIALS_DIR={credentials}"],
    }


def calls(host) -> list[str]:
    return host["log"].read_text().splitlines()


@pytest.mark.parametrize("pi_dir", ["/", "/home/pi", "..", "a/../b", "~", "~/x", "-rf", "a b",
                                    "x;rm", "$(id)"])
def test_unsafe_pi_directories_are_refused_before_ssh(host, pi_dir):
    for target in ("pi-sync", "pi-setup", "pi-status"):
        result = make(target, *PI, f"PI_DIR={pi_dir}", extra=host["env"])
        assert result.returncode == 2 and "PI_DIR must be a plain directory" in result.stderr
    assert calls(host) == []


@pytest.mark.parametrize("user,pi_host", [("pi", "-oProxyCommand=x"), ("pi;rm", "h"),
                                           ("pi", "h x"), ("", "h")])
def test_unsafe_ssh_targets_are_refused_before_ssh(host, user, pi_host):
    result = make("pi-sync", f"PI_USER={user}", f"PI_HOST={pi_host}", extra=host["env"])
    assert result.returncode == 2
    assert calls(host) == []


def test_pi_sync_copies_the_tree_without_local_files(host):
    result = make("pi-sync", *PI, extra=host["env"])
    assert result.returncode == 0, result.stderr
    ssh, rsync = calls(host)
    assert ssh == "ssh pi@raspberrypi.local mkdir -p glucose-guardian"
    assert rsync.startswith("rsync -a --delete ")
    assert rsync.endswith(" pi@raspberrypi.local:glucose-guardian/")
    for exclude in (".venv", "__pycache__", "config/local.*", "bench/reports"):
        assert f"--exclude {exclude}" in rsync
    for source in ("Makefile", "README.md", "backend", "bench", "config", "deployment", "docs"):
        assert f" {ROOT}/{source} " in rsync
    assert f"{ROOT}/firmware" not in rsync


@pytest.mark.parametrize("target", ["dexcom-check", "dexcom-credentials"])
def test_dexcom_targets_refuse_without_a_terminal(host, target):
    result = make(target, *host["args"], extra=host["env"])
    assert result.returncode == 2 and "needs an interactive terminal" in result.stderr
    assert calls(host) == []


def run_in_terminal(root: Path, args: list[str], env: dict[str, str],
                    answer: str | None) -> tuple[int, str]:
    pid, descriptor = pty.fork()
    if pid == 0:
        os.chdir(root)
        os.execvpe("make", ["make", "-C", str(root), *args], clean_environment(env))
    output = b""
    answered = answer is None
    deadline = time.monotonic() + 30
    while True:
        if time.monotonic() > deadline:
            os.killpg(pid, signal.SIGKILL)
            break
        ready, _, _ = select.select([descriptor], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(descriptor, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if not answered and b'Type "' in output:
            os.write(descriptor, f"{answer}\n".encode())
            answered = True
    _, status = os.waitpid(pid, 0)
    os.close(descriptor)
    return os.waitstatus_to_exitcode(status), output.decode(errors="replace")


def provision(host) -> None:
    host["credentials"].mkdir(mode=0o700)
    for name in ("username", "password"):
        (host["credentials"] / f"dexcom-{name}.cred").write_text("encrypted\n")


@pytest.mark.parametrize("answer", ["", "yes", "contact Dexcom", "contact dexcom please"])
def test_dexcom_check_without_the_exact_confirmation_contacts_nothing(host, answer):
    provision(host)
    code, output = run_in_terminal(ROOT, ["dexcom-check", *host["args"]], host["env"], answer)
    assert code == 2 and "cancelled: nothing was done" in output
    assert not any(call.startswith("systemd-run") for call in calls(host))


def test_dexcom_check_runs_the_check_once_and_never_retries(host):
    provision(host)
    code, output = run_in_terminal(ROOT, ["dexcom-check", *host["args"]], host["env"],
                                   "contact dexcom")
    runs = [call for call in calls(host) if call.startswith("systemd-run")]
    assert code != 0 and "do not retry" in output
    assert len(runs) == 1
    run = runs[0].split()
    credentials = host["credentials"]
    assert run[1:4] == ["--pipe", "--wait", f"--uid={os.getuid()}"]
    assert f"LoadCredentialEncrypted=dexcom-username:{credentials}/dexcom-username.cred" in run
    assert f"LoadCredentialEncrypted=dexcom-password:{credentials}/dexcom-password.cred" in run
    assert run[-4:-1] == ["-m", "glucose_guardian.share_check", "--config"]


def test_dexcom_check_refuses_without_credentials(host):
    code, output = run_in_terminal(ROOT, ["dexcom-check", *host["args"]], host["env"], None)
    assert code == 2 and "run make dexcom-credentials" in output
    assert not any(call.startswith("systemd-run") for call in calls(host))


def test_credentials_are_encrypted_from_the_prompt_and_never_passed_on(host):
    code, output = run_in_terminal(ROOT, ["dexcom-credentials", *host["args"]], host["env"],
                                   None)
    assert code == 0, output
    credentials = host["credentials"]
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o700
    for name in ("username", "password"):
        written = credentials / f"dexcom-{name}.cred"
        assert written.read_text() == "encrypted\n"
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert not list(credentials.glob("*.new"))
    log = host["log"].read_text()
    assert SECRET not in log and SECRET not in host["environments"].read_text()
    assert SECRET not in output
    assert log.count("systemd-ask-password -n") == 2
    asked = len(calls(host))
    code, output = run_in_terminal(ROOT, ["dexcom-credentials", *host["args"]], host["env"],
                                   None)
    assert code == 0 and output.count("exists; kept") == 2
    assert "systemd-ask-password" not in "\n".join(calls(host)[asked:])


@pytest.fixture
def staged_root(tmp_path):
    root = tmp_path / "root"
    (root / "bench" / "out").mkdir(parents=True)
    shutil.copy(MAKEFILE, root / "Makefile")
    shutil.copy(ROOT / "bench" / "Makefile", root / "bench" / "Makefile")
    (root / "bench" / "out" / "SHA256SUMS").write_text("")
    return root


@pytest.mark.parametrize("variant,answer", [("production", "yes"),
                                            ("production", "flash watchdog-test"),
                                            ("watchdog-test", "flash production")])
def test_firmware_flash_without_the_exact_confirmation_writes_nothing(host, staged_root,
                                                                      variant, answer):
    code, output = run_in_terminal(staged_root, ["firmware-flash", f"SERIAL_PORT={PORT}",
                                                 f"VARIANT={variant}", *host["args"]],
                                   host["env"], answer)
    assert code == 2 and "cancelled: nothing was done" in output
    assert f'Type "flash {variant}"' in output
    assert calls(host) == []


def test_firmware_flash_refuses_an_unknown_variant_and_a_missing_terminal(host, staged_root):
    unknown = make("firmware-flash", f"SERIAL_PORT={PORT}", "VARIANT=latest", *host["args"],
                   root=staged_root, extra=host["env"])
    assert unknown.returncode == 2 and "VARIANT must be production or watchdog-test" in (
        unknown.stderr)
    batch = make("firmware-flash", f"SERIAL_PORT={PORT}", "VARIANT=production", *host["args"],
                 root=staged_root, extra=host["env"])
    assert batch.returncode == 2 and "needs an interactive terminal" in batch.stderr
    assert calls(host) == []
