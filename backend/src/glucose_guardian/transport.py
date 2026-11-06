"""Hardware boundary. No serial device is opened; the simulated transport is a double."""
import math
from collections.abc import Callable
from typing import Protocol

from .alarms import State


class AlarmTransport(Protocol):
    def send(self, state: State) -> None: ...
    def heartbeat_age(self, monotonic_s: float) -> float: ...
    def take_button_presses(self) -> int: ...


class SimulatedTransport:
    def __init__(self, *, heartbeat_ok: Callable[[float], bool]) -> None:
        self._heartbeat_ok = heartbeat_ok
        self._last_heartbeat_s = -math.inf
        self._presses = 0
        self.last_sent: State | None = None
        self.fail = False

    def press(self) -> None:
        self._presses += 1

    def take_button_presses(self) -> int:
        presses, self._presses = self._presses, 0
        return presses

    def send(self, state: State) -> None:
        if self.fail:
            raise RuntimeError("simulated transport failure")
        self.last_sent = state

    def heartbeat_age(self, monotonic_s: float) -> float:
        if self._heartbeat_ok(monotonic_s):
            self._last_heartbeat_s = monotonic_s
        return monotonic_s - self._last_heartbeat_s
