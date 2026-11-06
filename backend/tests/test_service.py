import os
import signal
import subprocess
import sys
from pathlib import Path

from harness import write_config as write

from glucose_guardian import service

SRC = Path(__file__).resolve().parents[1] / "src"


def test_service_starts_and_stops_cleanly_on_sigterm(tmp_path):
    config = write(tmp_path, ("tick_s = 1", "tick_s = 0.05"))
    process = subprocess.Popen(
        [sys.executable, "-m", "glucose_guardian.service", "--config", str(config)],
        stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(SRC)})
    try:
        assert "simulated runtime started: scenario=normal" in process.stderr.readline()
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
