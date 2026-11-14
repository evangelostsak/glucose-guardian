import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness import EXAMPLE_SNAPSHOT, Harness, write_config
from PIL import Image
from processes import SRC, Watched

from glucose_guardian import screen
from glucose_guardian.alarms import GlucoseLevel, State
from glucose_guardian.config import load_config
from glucose_guardian.dexcom_share import ShareStatus, Trend
from glucose_guardian.display import FanOutDisplay, MemoryDisplay, Source, build_frame
from glucose_guardian.notify import Notifier, notifier_from_environment
from glucose_guardian.providers import Reading
from glucose_guardian.screen_model import ScreenSettings, ScreenState, restarting
from glucose_guardian.screen_render import HEIGHT, WIDTH, Renderer
from glucose_guardian.shutdown import SIGNALS
from glucose_guardian.simulation import SCENARIOS
from glucose_guardian.snapshot import SnapshotDisplay, encode, write_snapshot

NOW = datetime(2026, 1, 1, 3, 14, tzinfo=UTC)
SETTINGS = ScreenSettings(snapshot_max_age_s=5, last_known_cutoff_s=1800,
                          source=Source.DEXCOM_SHARE, outputs_enabled=False)
FORBIDDEN = {"glucose_guardian.share_client", "glucose_guardian.credentials",
             "glucose_guardian.share_state", "glucose_guardian.service",
             "glucose_guardian.serial_transport", "glucose_guardian.runtime", "ssl",
             "http.client", "urllib.request", "serial"}


def snapshot_bytes(written_s: float, mg_dl: float = 123) -> bytes:
    state = State(GlucoseLevel.NORMAL, True, frozenset(), False, False)
    frame = build_frame(state, Reading(mg_dl, NOW, Trend.FLAT), NOW,
                        source=Source.DEXCOM_SHARE, source_status=ShareStatus.OK,
                        outputs_enabled=False)
    return encode(frame, written_monotonic_s=written_s, max_age_s=600)


class Recorder:
    def __init__(self) -> None:
        self.models = []
        self.fail = False

    def render(self, model):
        if self.fail:
            raise RuntimeError("synthetic render failure")
        self.models.append(model)
        return type("Rendering", (), {"image": model})()


class Sink:
    def __init__(self) -> None:
        self.shown = []

    def show(self, image) -> None:
        self.shown.append(image)


class Messages:
    def __init__(self) -> None:
        self.sent = []

    def notify(self, message: str) -> None:
        self.sent.append(message)


def drive(path: Path, steps, *, notifier=None, renderer=None):
    clock = [100.0]
    renderer = renderer or Recorder()
    sink = Sink()
    remaining = list(steps)

    def wait(timeout_s: float) -> bool:
        clock[0] += timeout_s
        if not remaining:
            return True
        remaining.pop(0)(clock[0])
        return False

    screen.run(snapshot_file=path, settings=SETTINGS, refresh_s=1.0, renderer=renderer,
               sink=sink, notifier=notifier, monotonic=lambda: clock[0],
               now_local=lambda: NOW, wait=wait)
    return renderer, sink


def test_loop_redraws_only_on_change_and_recovers_from_bad_snapshots(tmp_path, caplog):
    path = tmp_path / "display.json"
    notifier = Messages()

    def valid(now_s):
        write_snapshot(path, snapshot_bytes(now_s))

    def corrupt(now_s):
        path.write_text("{")

    with caplog.at_level(logging.INFO, logger="glucose_guardian.screen"):
        renderer, sink = drive(path, [valid, valid, valid, corrupt, valid,
                                      lambda now_s: path.unlink()], notifier=notifier)
    states = [model.state for model in renderer.models]
    assert states == [ScreenState.NO_DATA, ScreenState.LIVE, ScreenState.NO_DATA,
                      ScreenState.LIVE, ScreenState.NO_DATA]
    assert [model.detail for model in renderer.models if model.state is ScreenState.NO_DATA] \
        == ["No recent display data", "Display data invalid", "No recent display data"]
    assert len(sink.shown) == len(renderer.models)
    assert notifier.sent[0] == "READY=1" and notifier.sent[-1] == "STOPPING=1"
    assert notifier.sent.count("WATCHDOG=1") == 7
    assert "123" not in caplog.text and "screen state live" in caplog.text


def test_a_snapshot_that_stops_updating_turns_into_no_data(tmp_path):
    path = tmp_path / "display.json"
    write_snapshot(path, snapshot_bytes(100.0))
    renderer, _ = drive(path, [lambda now_s: None] * 6)
    assert [model.state for model in renderer.models] == [ScreenState.LIVE,
                                                          ScreenState.NO_DATA]
    assert renderer.models[-1].detail == "No recent display data"
    assert (renderer.models[-1].value, renderer.models[-1].trend) == (None, None)


def test_a_render_failure_stops_the_loop_before_the_watchdog_ping(tmp_path):
    renderer = Recorder()
    renderer.fail = True
    notifier = Messages()
    with pytest.raises(RuntimeError, match="synthetic render failure"):
        drive(tmp_path / "display.json", [], notifier=notifier, renderer=renderer)
    assert notifier.sent == []


def test_png_sink_writes_atomically(tmp_path, monkeypatch):
    path = tmp_path / "screen.png"
    sink = screen.PngSink(path)
    sink.show(Image.new("RGB", (WIDTH, HEIGHT)))
    with Image.open(path) as image:
        assert image.size == (WIDTH, HEIGHT)

    def broken(self, *args, **kwargs):
        raise OSError("synthetic save failure")

    monkeypatch.setattr(Image.Image, "save", broken)
    with pytest.raises(OSError):
        sink.show(Image.new("RGB", (WIDTH, HEIGHT)))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["screen.png"]


def test_blank_shows_display_restarting_and_exits(tmp_path):
    config = write_config(tmp_path)
    png = tmp_path / "screen.png"
    assert screen.main(["--config", str(config), "--png", str(png), "--blank"]) == 0
    display = load_config(config).display
    assert display is not None
    expected = Renderer(display.font_file).render(restarting()).image
    with Image.open(png) as image:
        assert image.convert("RGB").tobytes() == expected.tobytes()


def test_display_process_needs_snapshot_settings(tmp_path, caplog):
    text = write_config(tmp_path).read_text()
    config = tmp_path / "log.toml"
    config.write_text(text[:text.index("[display]")] + '[display]\nkind = "log"\n'
                      + text[text.index("[hardware]"):])
    assert screen.main(["--config", str(config), "--png", str(tmp_path / "s.png")]) == 2
    assert 'kind must be "snapshot"' in caplog.text


def test_display_process_refuses_a_missing_font(tmp_path, caplog):
    config = write_config(tmp_path, (
        'font_file = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"',
        'font_file = "/nonexistent/font.ttf"'))
    assert screen.main(["--config", str(config), "--png", str(tmp_path / "s.png")]) == 2
    assert "font_file cannot be loaded" in caplog.text


def test_notifier_sends_datagrams_to_path_and_abstract_sockets(tmp_path):
    for address, bind in ((str(tmp_path / "notify"), str(tmp_path / "notify")),
                          (f"@glucose-guardian-test-{os.getpid()}",
                           f"\0glucose-guardian-test-{os.getpid()}")):
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(bind)
            Notifier(address).notify("WATCHDOG=1")
            assert server.recv(64) == b"WATCHDOG=1"
    assert notifier_from_environment({}) is None
    with pytest.raises(ValueError):
        Notifier("relative/socket")


def test_display_modules_load_no_dexcom_client_credentials_state_or_network():
    probe = ("import sys, glucose_guardian.screen, glucose_guardian.preview, "
             "glucose_guardian.screen_render; print('\\n'.join(sys.modules))")
    loaded = set(subprocess.run([sys.executable, "-c", probe], check=True, text=True,
                                capture_output=True, timeout=60,
                                env={**os.environ, "PYTHONPATH": str(SRC)}).stdout.split())
    assert "glucose_guardian.screen" in loaded
    assert loaded & FORBIDDEN == set()


def test_a_failing_snapshot_write_never_changes_alarm_evaluation(tmp_path):
    scenario = SCENARIOS["simultaneous_faults"]
    end_s = sum(segment.duration_s for segment in scenario.segments)
    reference, broken = Harness(scenario), Harness(scenario)
    memory = MemoryDisplay()
    broken.runtime._display = FanOutDisplay((
        memory, SnapshotDisplay(path=tmp_path / "absent" / "display.json",
                                monotonic=lambda: broken.t, max_age_s=600)))
    ticks = 0
    while reference.t <= end_s:
        assert broken.run_until(broken.t) == reference.run_until(reference.t)
        assert broken.transport.last_sent == reference.transport.last_sent
        ticks += 1
    assert broken.runtime.counts["display_errors"] == ticks
    assert reference.runtime.counts["display_errors"] == 0
    assert memory.last_frame is not None


def service_config(tmp_path: Path) -> Path:
    return write_config(tmp_path, ("tick_s = 1", "tick_s = 0.05"),
                        ("snapshot_max_age_s = 5", "snapshot_max_age_s = 1"),
                        ("refresh_s = 1", "refresh_s = 0.1"))


def test_expect_fails_in_time_when_a_line_never_comes():
    quiet = Watched("-m", "glucose_guardian.screen", "--help")
    try:
        started = time.monotonic()
        with pytest.raises(AssertionError, match="not seen within"):
            quiet.expect("screen state live", 1)
        assert time.monotonic() - started < 5
    finally:
        quiet.kill()


def test_display_process_follows_the_runtime_end_to_end(tmp_path):
    config = service_config(tmp_path)
    assert str(tmp_path / "display.json") in config.read_text()
    assert EXAMPLE_SNAPSHOT not in config.read_text()
    notify_path = tmp_path / "notify"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(notify_path))
    receiver.settimeout(10)
    png = tmp_path / "screen.png"
    screen_args = ("--config", str(config), "--png", str(png))
    runtime = Watched("-m", "glucose_guardian.service", "--config", str(config))
    display = Watched("-m", "glucose_guardian.screen", *screen_args,
                      env={"NOTIFY_SOCKET": str(notify_path)})
    try:
        runtime.expect("runtime started", 10)
        display.expect("screen state live", 10)
        assert receiver.recv(64) == b"READY=1"
        with Image.open(png) as image:
            assert image.size == (WIDTH, HEIGHT)
        runtime.stop(signal.SIGKILL)
        killed = time.monotonic()
        noticed = display.expect("screen state no_data (No recent display data)", 10)
        assert noticed - killed <= 1 + 0.1 + 1.5
        runtime = Watched("-m", "glucose_guardian.service", "--config", str(config))
        display.expect("screen state live", 10)
        display.stop(signal.SIGKILL)
        written = screen.load(tmp_path / "display.json").written_monotonic_s
        time.sleep(0.5)
        assert screen.load(tmp_path / "display.json").written_monotonic_s > written
        rest = runtime.stop(signal.SIGTERM)
        assert runtime.process.returncode == 0 and "display error" not in rest
        display = Watched("-m", "glucose_guardian.screen", *screen_args,
                          env={"NOTIFY_SOCKET": str(notify_path)})
        display.expect("screen state no_data (No recent display data)", 10)
        display.stop(signal.SIGTERM)
        assert display.process.returncode == 0
        received = []
        receiver.settimeout(0.5)
        while True:
            try:
                received.append(receiver.recv(64))
            except TimeoutError:
                break
        assert received[-1] == b"STOPPING=1" and b"WATCHDOG=1" in received
    finally:
        for watched in (runtime, display):
            watched.kill()
        receiver.close()


@pytest.mark.parametrize("sig", SIGNALS)
def test_a_signal_ends_the_display_during_a_long_refresh_wait(tmp_path, sig):
    config = write_config(tmp_path, ("snapshot_max_age_s = 5", "snapshot_max_age_s = 90"),
                          ("refresh_s = 1", "refresh_s = 60"))
    display = Watched("-m", "glucose_guardian.screen", "--config", str(config),
                      "--png", str(tmp_path / "screen.png"))
    try:
        display.expect("screen state no_data", 10)
        rest = display.stop(sig)
    finally:
        display.kill()
    assert display.process.returncode == screen.EXIT_STOPPED
    assert "display stopped" in rest


def test_display_stops_cleanly_in_repeated_rapid_start_stop_cycles(tmp_path):
    screen_args = ("--config", str(service_config(tmp_path)), "--png", str(tmp_path / "s.png"))
    for trial in range(6):
        display = Watched("-m", "glucose_guardian.screen", *screen_args)
        try:
            display.expect("screen state", 10)
            rest = display.stop(SIGNALS[trial % 2])
        finally:
            display.kill()
        assert display.process.returncode == screen.EXIT_STOPPED
        assert "display stopped" in rest


def test_screen_settings_follow_the_configuration(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.display is not None
    assert screen.screen_settings(config, config.display) == replace(
        SETTINGS, source=Source.SIMULATED, snapshot_max_age_s=5, last_known_cutoff_s=1800)
