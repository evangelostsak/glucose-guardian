import base64
import json
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
from dataclasses import replace
from pathlib import Path

import pytest
from harness import ROOT

from glucose_guardian.share_state import fresh_state, write_state

pytestmark = pytest.mark.skipif(shutil.which("make") is None or sys.platform != "linux",
                                reason="the Makefile targets Linux hosts with GNU make")

MAKEFILE = ROOT / "Makefile"
FAKE_VENV = "/fake/venv"
PORT = "/dev/serial/by-id/usb-Arduino_Uno_SYNTHETIC-if00"
SECRET = "synthetic-secret-typed-at-the-prompt"
OLD_USERNAME = "synthetic-publisher-old"
OLD_PASSWORD = "synthetic-password-old"
NEW_VALUE = "synthetic-replacement-value"
PI = ["PI_USER=pi", "PI_HOST=raspberrypi.local"]
EVERY_VARIABLE = [f"VENV={FAKE_VENV}", f"SERIAL_PORT={PORT}", "VARIANT=production",
                  "CREDENTIAL=password", *PI]
DEXCOM_CONTACT = "glucose_guardian.share_check"
SYSTEMD_RUN = "sudo systemd-run"
SYSTEMD_RUN_TARGETS = {"dexcom-check", "dexcom-credentials-check", "dexcom-credentials-rotate"}
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
    ("display-preview", [], [f"{FAKE_VENV}/bin/python -m glucose_guardian.preview --config "
                             f"{ROOT}/config/example.toml --out {ROOT}/build/display-preview"]),
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
    ("dexcom-status", [], [f"-m glucose_guardian.share_state --config "
                           f"{ROOT}/config/local.dexcom.toml status"]),
    ("dexcom-credentials-check", [], ["sudo systemd-run --pipe --wait",
                                      "-m glucose_guardian.credentials"]),
    ("dexcom-credentials-rotate", ["CREDENTIAL=password"],
     ['Type "replace dexcom-password" to continue:',
      "printf '%s' \"$first\" | sudo systemd-creds encrypt --name=dexcom-password - \"$new\"",
      "LoadCredentialEncrypted=dexcom-password:\"$pass\"", "-m glucose_guardian.credentials",
      'sudo mv -f "$new" "$current"']),
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
        writes = any(marker in output for marker in FIRMWARE_WRITE)
        assert (DEXCOM_CONTACT in output) == (target == "dexcom-check"), target
        assert (SYSTEMD_RUN in output) == (target in SYSTEMD_RUN_TARGETS), target
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


FAKE_PRELUDE = '''import base64, json, os, subprocess, sys, tempfile
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join([{name!r}, *sys.argv[1:]]) + "\\n")
'''
FAKE_ASK_PASSWORD = f'''answers = os.environ.get("FAKE_ANSWERS")
if answers is None:
    sys.stdout.write({SECRET!r})
else:
    lines = Path(answers).read_text().split("\\n")
    Path(answers).write_text("\\n".join(lines[1:]))
    sys.stdout.write(lines[0])
'''
FAKE_CREDS = '''with open(os.environ["FAKE_ENVIRONMENTS"], "a") as dump:
    dump.write("\\n".join(f"{key}={value}" for key, value in os.environ.items()) + "\\n")
data = sys.stdin.buffer.read()
if os.environ.get("FAKE_ENCRYPT_FAIL"):
    sys.exit(1)
name = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--name="))
Path(sys.argv[-1]).write_text(json.dumps({"name": name,
                                          "data": base64.b64encode(data).decode()}))
'''
FAKE_RUN = '''if os.environ.get("FAKE_RUN") != "emulate":
    sys.exit(1)
args, sources, index = sys.argv[1:], {}, 0
while args[index].startswith("-"):
    if args[index] == "-p":
        key, _, value = args[index + 1].partition("=")
        if key == "LoadCredentialEncrypted":
            name, _, source = value.partition(":")
            sources[name] = source
        index += 2
    else:
        index += 1
command = args[index:]
if "glucose_guardian.share_check" in command:
    sys.exit(99)
directory = Path(tempfile.mkdtemp(dir=os.environ["FAKE_TMP"]))
for name, source in sources.items():
    record = json.loads(Path(source).read_text())
    if record["name"] != name:
        sys.exit(f"credential {name} was encrypted under another name")
    (directory / name).write_bytes(base64.b64decode(record["data"]))
    (directory / name).chmod(0o400)
environment = os.environ | {"CREDENTIALS_DIRECTORY": str(directory)}
sys.exit(subprocess.run(command, env=environment).returncode)
'''


def fake_tools(directory: Path) -> Path:
    bin_dir = directory / "fakebin"
    bin_dir.mkdir()
    shell = {"sudo": '[ "$1" = "-v" ] && exit 0\nexec "$@"', "ssh": "exit 0",
             "rsync": "exit 0", "avrdude": "exit 0"}
    for name, body in shell.items():
        path = bin_dir / name
        path.write_text(f'#!/bin/bash\nprintf "%s\\n" "{name} $*" >> "$FAKE_LOG"\n{body}\n')
        path.chmod(0o755)
    python = {"systemd-ask-password": FAKE_ASK_PASSWORD, "systemd-creds": FAKE_CREDS,
              "systemd-run": FAKE_RUN}
    for name, body in python.items():
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n" + FAKE_PRELUDE.format(name=name) + body)
        path.chmod(0o755)
    return bin_dir


def fake_venv(directory: Path) -> Path:
    python = directory / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}/backend/src" exec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    return python.parent.parent


def dexcom_config(path: Path, state_dir: Path) -> Path:
    example = (ROOT / "config" / "example-dexcom-share.toml").read_text()
    path.write_text(example.replace("/var/lib/glucose-guardian", str(state_dir)))
    return path


@pytest.fixture
def host(tmp_path):
    log = tmp_path / "calls.log"
    log.touch()
    environments = tmp_path / "environments.log"
    environments.touch()
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    bin_dir = fake_tools(tmp_path)
    state_dir = tmp_path / "state"
    config = dexcom_config(tmp_path / "local.dexcom.toml", state_dir)
    credentials = tmp_path / "credentials"
    venv = fake_venv(tmp_path)
    return {
        "log": log, "environments": environments, "credentials": credentials,
        "config": config, "state_file": state_dir / "dexcom-share-state.json",
        "env": {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
                "FAKE_ENVIRONMENTS": str(environments), "FAKE_TMP": str(fake_tmp)},
        "base": [f"VENV={venv}", f"DEXCOM_CONFIG={config}", f"CREDENTIALS_DIR={credentials}"],
        "args": [f"VENV={venv}", f"DEXCOM_CONFIG={config}", f"STATE_DIR={state_dir}",
                 f"CREDENTIALS_DIR={credentials}"],
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


def fake_credential(name: str, value: str) -> str:
    return json.dumps({"name": name, "data": base64.b64encode(value.encode()).decode()})


def decrypted(path: Path) -> str:
    return base64.b64decode(json.loads(path.read_text())["data"]).decode()


def provision(host) -> None:
    host["credentials"].mkdir(mode=0o700)
    for name, value in (("username", OLD_USERNAME), ("password", OLD_PASSWORD)):
        written = host["credentials"] / f"dexcom-{name}.cred"
        written.write_text(fake_credential(f"dexcom-{name}", value))
        written.chmod(0o600)


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
        assert json.loads(written.read_text())["name"] == f"dexcom-{name}"
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


def blocked_state(state_file: Path) -> None:
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_state(state_file, replace(fresh_state("boot-a"), blocked_reason="AccountPasswordInvalid",
                                    blocked_utc="2026-01-01T00:00:00+00:00"))


def test_dexcom_status_reads_the_state_file_named_in_the_config(host):
    blocked_state(host["state_file"])
    result = make("dexcom-status", *host["base"], extra=host["env"])
    assert result.returncode == 0, result.stderr
    assert f"state file: {host['state_file']}" in result.stdout
    assert "blocked: AccountPasswordInvalid" in result.stdout


def test_a_state_dir_that_matches_the_config_is_accepted(host):
    result = make("dexcom-status", *host["args"], extra=host["env"])
    assert result.returncode == 0 and f"state file: {host['state_file']}" in result.stdout


@pytest.mark.parametrize("target", ["dexcom-status", "dexcom-check", "dexcom-config"])
def test_a_state_dir_that_disagrees_with_the_config_is_refused(host, tmp_path, target):
    other = tmp_path / "other-state"
    result = make(target, *host["base"], f"STATE_DIR={other}", extra=host["env"])
    assert result.returncode == 2
    assert f"STATE_DIR={other} does not match {host['config']}" in result.stderr
    assert "the state file always comes from DEXCOM_CONFIG" in result.stderr
    assert not other.exists() and not host["state_file"].exists()
    assert calls(host) == []


def test_dexcom_check_preflight_and_share_check_use_the_same_config(host):
    provision(host)
    blocked_state(host["state_file"])
    code, output = run_in_terminal(ROOT, ["dexcom-check", *host["base"]], host["env"],
                                   "contact dexcom")
    assert f"state file: {host['state_file']}" in output
    assert "blocked: AccountPasswordInvalid" in output
    (run,) = [call for call in calls(host) if call.startswith("systemd-run")]
    assert run.endswith(f"-m glucose_guardian.share_check --config {host['config']}")


def test_dexcom_config_creates_and_reports_the_configured_state(host, tmp_path):
    config, state_dir = tmp_path / "generated.toml", tmp_path / "generated-state"
    venv = host["base"][0]
    created = make("dexcom-config", venv, f"DEXCOM_CONFIG={config}", f"STATE_DIR={state_dir}",
                   extra=host["env"])
    assert created.returncode == 0, created.stderr
    assert f"state file: {state_dir}/dexcom-share-state.json" in created.stdout
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    text = config.read_text()
    kept = make("dexcom-config", venv, f"DEXCOM_CONFIG={config}", extra=host["env"])
    assert kept.returncode == 0 and "exists; kept" in kept.stdout
    assert f"state file: {state_dir}/dexcom-share-state.json" in kept.stdout
    assert config.read_text() == text


def test_status_reports_the_state_file_named_in_the_config(host):
    blocked_state(host["state_file"])
    result = make("status", *host["base"], extra=host["env"])
    assert f"dexcom state: {host['state_file']}" in result.stdout


def with_answers(host, tmp_path: Path, *values: str) -> dict[str, str]:
    answers = tmp_path / "answers"
    answers.write_text("\n".join(values))
    return host["env"] | {"FAKE_ANSWERS": str(answers), "FAKE_RUN": "emulate"}


def rotate(host, env: dict[str, str], credential: str, answer: str | None) -> tuple[int, str]:
    return run_in_terminal(ROOT, ["dexcom-credentials-rotate", f"CREDENTIAL={credential}",
                                  *host["base"]], env, answer)


def assert_never_exposed(host, output: str, *values: str) -> None:
    for value in filter(None, values):
        assert value not in host["log"].read_text()
        assert value not in host["environments"].read_text()
        assert value not in output


@pytest.mark.parametrize("credential,other", [("password", "username"),
                                              ("username", "password")])
def test_rotation_swaps_in_the_new_credential_only_after_validation(host, tmp_path, credential,
                                                                    other):
    provision(host)
    kept = (host["credentials"] / f"dexcom-{other}.cred").read_text()
    env = with_answers(host, tmp_path, NEW_VALUE, NEW_VALUE)
    code, output = rotate(host, env, credential, f"replace dexcom-{credential}")
    assert code == 0, output
    assert f"dexcom-{credential} replaced" in output
    replaced = host["credentials"] / f"dexcom-{credential}.cred"
    assert decrypted(replaced) == NEW_VALUE
    assert json.loads(replaced.read_text())["name"] == f"dexcom-{credential}"
    assert stat.S_IMODE(replaced.stat().st_mode) == 0o600
    assert (host["credentials"] / f"dexcom-{other}.cred").read_text() == kept
    assert not list(host["credentials"].glob("*.new"))
    (run,) = [call for call in calls(host) if call.startswith("systemd-run")]
    assert f"LoadCredentialEncrypted=dexcom-{credential}:{replaced}.new" in run
    current = host["credentials"] / f"dexcom-{other}.cred"
    assert f"LoadCredentialEncrypted=dexcom-{other}:{current}" in run
    assert run.endswith("-m glucose_guardian.credentials")
    assert_never_exposed(host, output, NEW_VALUE, OLD_USERNAME, OLD_PASSWORD)


@pytest.mark.parametrize("entries,failure,message", [
    ((NEW_VALUE, NEW_VALUE + "-typo"), {}, "the two entries differ"),
    (("x" * 300, "x" * 300), {}, "validation failed; dexcom-password is unchanged"),
    (("", ""), {}, "validation failed; dexcom-password is unchanged"),
    ((NEW_VALUE, NEW_VALUE), {"FAKE_ENCRYPT_FAIL": "1"}, "Error"),
])
def test_a_failed_rotation_keeps_the_current_credential(host, tmp_path, entries, failure,
                                                        message):
    provision(host)
    current = host["credentials"] / "dexcom-password.cred"
    before = current.read_text()
    env = with_answers(host, tmp_path, *entries) | failure
    code, output = rotate(host, env, "password", "replace dexcom-password")
    assert code == 2 and message in output
    assert current.read_text() == before and decrypted(current) == OLD_PASSWORD
    assert not list(host["credentials"].glob("*.new"))
    assert_never_exposed(host, output, *entries)


@pytest.mark.parametrize("answer", ["", "yes", "replace dexcom-username",
                                    "replace dexcom-password now"])
def test_rotation_without_the_exact_confirmation_asks_for_nothing(host, tmp_path, answer):
    provision(host)
    before = (host["credentials"] / "dexcom-password.cred").read_text()
    env = with_answers(host, tmp_path, NEW_VALUE, NEW_VALUE)
    code, output = rotate(host, env, "password", answer)
    assert code == 2 and "cancelled: nothing was done" in output
    assert not any(call.startswith(("systemd-ask-password", "systemd-creds", "systemd-run"))
                   for call in calls(host))
    assert (host["credentials"] / "dexcom-password.cred").read_text() == before


def test_rotation_refuses_without_a_valid_credential_name_a_terminal_or_credentials(host,
                                                                                     tmp_path):
    missing = make("dexcom-credentials-rotate", *host["base"], extra=host["env"])
    assert missing.returncode == 2 and "CREDENTIAL is required" in missing.stderr
    both = make("dexcom-credentials-rotate", "CREDENTIAL=both", *host["base"], extra=host["env"])
    assert both.returncode == 2 and "CREDENTIAL must be username or password" in both.stderr
    batch = make("dexcom-credentials-rotate", "CREDENTIAL=password", *host["base"],
                 extra=host["env"])
    assert batch.returncode == 2 and "needs an interactive terminal" in batch.stderr
    assert calls(host) == []
    code, output = rotate(host, with_answers(host, tmp_path, NEW_VALUE, NEW_VALUE), "password",
                          "replace dexcom-password")
    assert code == 2 and "run make dexcom-credentials" in output
    assert not any(call.startswith("systemd-ask-password") for call in calls(host))


def test_credentials_check_decrypts_through_systemd_without_contacting_dexcom(host):
    provision(host)
    env = host["env"] | {"FAKE_RUN": "emulate"}
    valid = make("dexcom-credentials-check", *host["base"], extra=env)
    assert valid.returncode == 0, valid.stderr
    assert "both decrypt and are well-formed" in valid.stdout
    (run,) = [call for call in calls(host) if call.startswith("systemd-run")]
    assert run.endswith("-m glucose_guardian.credentials")
    password = host["credentials"] / "dexcom-password.cred"
    password.write_text(fake_credential("dexcom-username", OLD_PASSWORD))
    assert make("dexcom-credentials-check", *host["base"], extra=env).returncode == 2
    password.write_text(fake_credential("dexcom-password", "two\nlines"))
    rejected = make("dexcom-credentials-check", *host["base"], extra=env)
    assert rejected.returncode == 2 and "must be one non-empty line" in rejected.stderr
    assert OLD_PASSWORD not in rejected.stdout + rejected.stderr
