import os
import signal
import subprocess
import sys
from pathlib import Path

from harness import write_config as write

from glucose_guardian import service
from glucose_guardian.config import load_config
from glucose_guardian.serial_transport import SerialTransport
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
