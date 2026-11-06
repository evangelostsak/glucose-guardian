"""Fixed-rate supervisory loop. Alarm evaluation never waits on acquisition."""
import logging
import math
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from .alarms import AlarmMachine, Policy, State
from .config import RuntimeSettings
from .display import Display, Frame, build_frame
from .providers import GlucoseProvider, Reading
from .transport import AlarmTransport
from .worker import BoundedWorker, Done, Spawn, TimedOut

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, *, policy: Policy, settings: RuntimeSettings, provider: GlucoseProvider,
                 clock_check: Callable[[], bool], transport: AlarmTransport, display: Display,
                 wall_clock: Callable[[], datetime], monotonic: Callable[[], float],
                 acquisition_spawn: Spawn, clock_spawn: Spawn) -> None:
        self.machine = AlarmMachine(policy)
        self.counts: Counter[str] = Counter()
        self._tick_s = settings.tick_s
        self._provider = provider
        self._clock_check = clock_check
        self._transport = transport
        self._display = display
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self.acquisition: BoundedWorker[Reading | None] = BoundedWorker(
            interval_s=settings.fetch_interval_s, timeout_s=settings.fetch_timeout_s,
            spawn=acquisition_spawn)
        self.clock: BoundedWorker[bool] = BoundedWorker(
            interval_s=settings.clock_check_interval_s,
            timeout_s=settings.clock_check_timeout_s, spawn=clock_spawn)
        self._latest: Reading | None = None
        self._last_valid: Reading | None = None
        self._acquisition_ok = True
        self._clock_synchronized = False

    def run(self, wait: Callable[[float], bool]) -> None:
        deadline_s = self._monotonic()
        while True:
            self.tick()
            deadline_s += self._tick_s
            now_s = self._monotonic()
            if now_s >= deadline_s:
                missed = math.floor((now_s - deadline_s) / self._tick_s) + 1
                self.counts["overruns"] += missed
                deadline_s += missed * self._tick_s
            if wait(deadline_s - now_s):
                return

    def tick(self) -> State:
        now = self._wall_clock()
        monotonic_s = self._monotonic()
        self.counts["ticks"] += 1
        if self._button_presses() > 0:
            self.machine.acknowledge_glucose(monotonic_s)
            self.machine.acknowledge_faults(monotonic_s)
        self._poll_acquisition(now, monotonic_s)
        self._poll_clock(monotonic_s)
        state = self.machine.update(
            self._latest, now=now, monotonic_s=monotonic_s,
            acquisition_ok=self._acquisition_ok,
            clock_synchronized=self._clock_synchronized,
            heartbeat_age_s=self._heartbeat_age(monotonic_s))
        if state.glucose_current:
            self._last_valid = self._latest
        self._send(state)
        self._render(build_frame(state, self._last_valid, now))
        return state

    def _poll_acquisition(self, now: datetime, monotonic_s: float) -> None:
        outcome = self.acquisition.poll(monotonic_s, lambda: self._provider.read(now))
        if isinstance(outcome, Done):
            self._acquisition_ok = True
            if outcome.value is not None:
                self._latest = outcome.value
        elif outcome is not None:
            self._acquisition_ok = False
            self.counts["fetch_timeouts" if isinstance(outcome, TimedOut)
                        else "fetch_failures"] += 1
            log.warning("acquisition failed: %s", outcome)

    def _poll_clock(self, monotonic_s: float) -> None:
        outcome = self.clock.poll(monotonic_s, self._clock_check)
        if isinstance(outcome, Done):
            self._clock_synchronized = outcome.value is True
        elif outcome is not None:
            self._clock_synchronized = False
            self.counts["clock_check_failures"] += 1
            log.warning("clock check failed: %s", outcome)

    def _button_presses(self) -> int:
        try:
            return self._transport.take_button_presses()
        except Exception as error:
            self._adapter_error("transport", error)
            return 0

    def _heartbeat_age(self, monotonic_s: float) -> float:
        try:
            return self._transport.heartbeat_age(monotonic_s)
        except Exception as error:
            self._adapter_error("transport", error)
            return math.inf

    def _send(self, state: State) -> None:
        try:
            self._transport.send(state)
        except Exception as error:
            self._adapter_error("transport", error)

    def _render(self, frame: Frame) -> None:
        try:
            self._display.render(frame)
        except Exception as error:
            self._adapter_error("display", error)

    def _adapter_error(self, component: str, error: Exception) -> None:
        self.counts[f"{component}_errors"] += 1
        log.warning("%s error: %s", component, type(error).__name__)
