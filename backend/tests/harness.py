import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glucose_guardian.alarms import Policy, State
from glucose_guardian.config import RuntimeSettings
from glucose_guardian.display import MemoryDisplay
from glucose_guardian.protocol import (
    MAX_LINE_BYTES,
    LineBuffer,
    Report,
    ResetCause,
    StateFrame,
    decode_state,
    encode_report,
    seq_newer,
)
from glucose_guardian.providers import SimulatedProvider
from glucose_guardian.runtime import Runtime
from glucose_guardian.simulation import Fetch, Scenario, ScenarioProvider, Segment
from glucose_guardian.transport import SimulatedTransport
from glucose_guardian.worker import run_inline

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.toml"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = Policy(urgent_low_mg_dl=55, low_mg_dl=70, high_mg_dl=250, hysteresis_mg_dl=5,
                rearm_mg_dl=10, max_age_s=600, max_future_skew_s=60,
                heartbeat_timeout_s=5, glucose_acknowledgement_s=60,
                fault_acknowledgement_s=60)
SETTINGS = RuntimeSettings(tick_s=1, fetch_interval_s=60, fetch_timeout_s=10,
                           clock_check_interval_s=60, clock_check_timeout_s=5)


def write_config(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    text = EXAMPLE.read_text()
    for old, new in replacements:
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "local.toml"
    path.write_text(text)
    return path


def unexpected_hang(seconds: float) -> None:
    raise AssertionError("hangs are modelled by the harness spawner")


def build_runtime(**changes: object) -> Runtime:
    parts: dict[str, object] = {
        "policy": POLICY, "settings": SETTINGS, "provider": SimulatedProvider((110,)),
        "clock_check": lambda: True,
        "transport": SimulatedTransport(heartbeat_ok=lambda t: True),
        "display": MemoryDisplay(), "wall_clock": lambda: NOW, "monotonic": lambda: 0.0,
        "acquisition_spawn": run_inline, "clock_spawn": run_inline,
    }
    assert set(changes) <= set(parts)
    return Runtime(**{**parts, **changes})


class Harness:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.t = 0.0
        self.pending: list[Callable[[], None]] = []
        self.transport = SimulatedTransport(heartbeat_ok=lambda t: self.segment(t).heartbeat)
        self.display = MemoryDisplay()
        self.runtime = Runtime(
            policy=POLICY, settings=SETTINGS,
            provider=ScenarioProvider(scenario, start_wall=NOW, elapsed=lambda: self.t,
                                      hang=unexpected_hang),
            clock_check=lambda: self.segment(self.t).clock_synchronized,
            transport=self.transport, display=self.display,
            wall_clock=lambda: NOW + timedelta(seconds=self.t), monotonic=lambda: self.t,
            acquisition_spawn=self.spawn_fetch, clock_spawn=run_inline)

    def segment(self, t: float) -> Segment:
        return self.scenario.segment_at(t)[1]

    def spawn_fetch(self, job: Callable[[], None]) -> None:
        if self.segment(self.t).fetch is Fetch.HANG:
            self.pending.append(job)
        else:
            job()

    def run_until(self, end_s: float) -> State:
        state = self.runtime.machine.state
        while self.t <= end_s:
            if self.pending and self.segment(self.t).fetch is not Fetch.HANG:
                for job in self.pending:
                    job()
                self.pending.clear()
            state = self.runtime.tick()
            self.t += SETTINGS.tick_s
        return state


class FakeUno:
    def __init__(self, boot: int) -> None:
        self.boot = boot
        self.seq = 0
        self.short_presses = 0
        self.long_presses = 0
        self.button_stuck = False
        self.cause = ResetCause.EXTERNAL
        self.silent = False
        self.corrupt = False
        self.accept_replays = False
        self.banner: bytes | None = None
        self.outbox = b""
        self.states: list[StateFrame] = []
        self._last: tuple[int, int] | None = None
        self._buffer = LineBuffer(MAX_LINE_BYTES)

    def reboot(self, boot: int, cause: ResetCause) -> None:
        self.boot, self.cause = boot, cause
        self.seq = self.short_presses = self.long_presses = 0
        self._last = None
        self._buffer.reset()
        if self.banner is not None:
            self.outbox += self.banner

    def _accepts(self, frame: StateFrame) -> bool:
        last = self._last
        fresh = (last is None or frame.session != last[0] or seq_newer(frame.seq, last[1])
                 or self.accept_replays)
        if fresh:
            self._last = (frame.session, frame.seq)
        return fresh

    def respond(self, data: bytes) -> bytes:
        replies = b""
        for item in self._buffer.feed(data):
            frame = decode_state(item) if isinstance(item, bytes) else item
            if not isinstance(frame, StateFrame):
                continue
            self.states.append(frame)
            if self.silent or not self._accepts(frame):
                continue
            self.seq = (self.seq + 1) % 0x10000
            reply = encode_report(Report(
                self.boot, self.seq, frame.session, frame.seq, self.cause, self.button_stuck,
                self.short_presses % 0x100, self.long_presses % 0x100))
            replies += reply.replace(b"GG1 R", b"GG1 r") if self.corrupt else reply
        return replies


def pump(fd: int, uno: FakeUno) -> None:
    try:
        data = os.read(fd, 4096)
    except BlockingIOError:
        data = b""
    reply, uno.outbox = uno.outbox + (uno.respond(data) if data else b""), b""
    if reply:
        os.write(fd, reply)
