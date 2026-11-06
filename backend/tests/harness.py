from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glucose_guardian.alarms import Policy, State
from glucose_guardian.config import RuntimeSettings
from glucose_guardian.display import MemoryDisplay
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
                heartbeat_timeout_s=5, acknowledgement_s=60)
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
