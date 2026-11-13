import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from harness import write_config as write
from share_harness import DEXCOM_EXAMPLE

from glucose_guardian import service
from glucose_guardian.config import load_config
from glucose_guardian.credentials import PASSWORD_FILE, USERNAME_FILE
from glucose_guardian.display import FanOutDisplay, LogDisplay, Source
from glucose_guardian.serial_transport import SerialTransport
from glucose_guardian.share_simulator import SYNTHETIC_PASSWORD, SYNTHETIC_USERNAME
from glucose_guardian.snapshot import read_snapshot
from glucose_guardian.transport import SimulatedTransport

SRC = Path(__file__).resolve().parents[1] / "src"


def test_service_starts_and_stops_cleanly_on_sigterm(tmp_path):
    config = write(tmp_path, ("tick_s = 1", "tick_s = 0.05"))
    process = subprocess.Popen(
        [sys.executable, "-m", "glucose_guardian.service", "--config", str(config)],
        stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(SRC)})
    try:
        assert "runtime started: scenario=normal transport=simulated" in process.stderr.readline()
        process.send_signal(signal.SIGTERM)
        _, rest = process.communicate(timeout=10)
    finally:
        process.kill()
    assert process.returncode == service.EXIT_STOPPED
    assert "runtime stopped after" in rest


def test_invalid_config_exits_with_config_error(tmp_path):
    assert service.main(["--config", str(tmp_path / "missing.toml")]) == service.EXIT_CONFIG


def test_runtime_crash_exits_with_crash_code_and_restores_signals(tmp_path, monkeypatch):
    def crash(self, wait):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(service.Runtime, "run", crash)
    before = signal.getsignal(signal.SIGTERM)
    assert service.main(["--config", str(write(tmp_path))]) == service.EXIT_CRASHED
    assert signal.getsignal(signal.SIGTERM) is before


def test_build_transport_follows_the_config(tmp_path):
    simulated = load_config(write(tmp_path))
    assert isinstance(service.build_transport(simulated, lambda t: True), SimulatedTransport)
    serial_config = load_config(write(tmp_path, ('kind = "simulated"', 'kind = "serial"\n'
                                                 'device = "/dev/null-uno"\n'
                                                 'reconnect_initial_s = 1\n'
                                                 'reconnect_max_s = 30')))
    transport = service.build_transport(serial_config, lambda t: True)
    assert isinstance(transport, SerialTransport) and not transport.connected


def dexcom_config(tmp_path, server: str, state_dir: Path) -> Path:
    text = DEXCOM_EXAMPLE.read_text().replace('server = "ous"', server).replace(
        "/var/lib/glucose-guardian", str(state_dir)).replace("tick_s = 1", "tick_s = 0.05").replace(
        "/run/glucose-guardian", str(tmp_path))
    path = tmp_path / "local-dexcom.toml"
    path.write_text(text)
    return path


def credentials_dir(tmp_path) -> Path:
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    for name, value in ((USERNAME_FILE, SYNTHETIC_USERNAME), (PASSWORD_FILE, SYNTHETIC_PASSWORD)):
        (directory / name).write_text(value + "\n")
        os.chmod(directory / name, 0o400)
    return directory


def test_dexcom_share_without_credentials_refuses_to_start(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    config = dexcom_config(tmp_path, 'server = "ous"', tmp_path)
    assert service.main(["--config", str(config)]) == service.EXIT_CONFIG
    assert "CREDENTIALS_DIRECTORY is not set" in caplog.text


def test_dexcom_share_without_a_state_directory_refuses_to_start(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials_dir(tmp_path)))
    config = dexcom_config(tmp_path, 'server = "ous"', tmp_path / "absent")
    assert service.main(["--config", str(config)]) == service.EXIT_CONFIG
    assert "does not exist" in caplog.text


def test_share_server_follows_the_configured_region(tmp_path):
    ous = load_config(dexcom_config(tmp_path, 'server = "ous"', tmp_path))
    assert service.share_server(ous).base_url.startswith("https://shareous1.dexcom.com/")
    local = load_config(dexcom_config(tmp_path, 'server = "simulator"\nsimulator_port = 8443',
                                      tmp_path))
    assert service.share_server(local).base_url == (
        "http://127.0.0.1:8443/ShareWebServices/Services/")


def test_service_reads_from_the_share_simulator_end_to_end(tmp_path, simulator):
    config = dexcom_config(tmp_path, f'server = "simulator"\nsimulator_port = {simulator.port}',
                           tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "glucose_guardian.service", "--config", str(config)],
        stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC),
             "CREDENTIALS_DIRECTORY": str(credentials_dir(tmp_path))})
    try:
        line = process.stderr.readline()
        assert "runtime started: provider=dexcom_share server=simulator" in line
        deadline = time.monotonic() + 15
        while simulator.requests["read"] < 1:
            assert time.monotonic() < deadline, "service never read from the simulator"
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        _, rest = process.communicate(timeout=10)
    finally:
        process.kill()
    assert process.returncode == service.EXIT_STOPPED
    assert (simulator.requests["authenticate"], simulator.requests["login"]) == (1, 1)
    for secret in (SYNTHETIC_USERNAME, SYNTHETIC_PASSWORD):
        assert secret not in line + rest
    assert json.loads((tmp_path / "dexcom-share-state.json").read_text())[
        "blocked_reason"] is None


def test_build_display_follows_the_display_table(tmp_path):
    snapshot = load_config(write(tmp_path))
    assert isinstance(service.build_display(snapshot), FanOutDisplay)
    text = write(tmp_path).read_text()
    log_only = tmp_path / "log.toml"
    log_only.write_text(text[:text.index("[display]")] + '[display]\nkind = "log"\n'
                        + text[text.index("[hardware]"):])
    assert isinstance(service.build_display(load_config(log_only)), LogDisplay)


def test_missing_snapshot_directory_refuses_to_start(tmp_path, caplog):
    config = write(tmp_path, (str(tmp_path / "display.json"),
                              str(tmp_path / "absent" / "display.json")))
    assert service.main(["--config", str(config)]) == service.EXIT_CONFIG
    assert "snapshot_file directory" in caplog.text


def test_simulated_service_writes_a_simulated_snapshot(tmp_path):
    config = write(tmp_path, ("tick_s = 1", "tick_s = 0.05"))
    process = subprocess.Popen(
        [sys.executable, "-m", "glucose_guardian.service", "--config", str(config)],
        stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(SRC)})
    try:
        assert "display=snapshot" in process.stderr.readline()
        deadline = time.monotonic() + 10
        while not (tmp_path / "display.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        written = read_snapshot(tmp_path / "display.json")
        process.send_signal(signal.SIGTERM)
        _, rest = process.communicate(timeout=10)
    finally:
        process.kill()
    assert (written.source, written.source_status, written.outputs_enabled) == (
        Source.SIMULATED, None, False)
    assert "110" not in rest
