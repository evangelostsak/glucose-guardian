"""Hardware boundary: transport protocol and the simulated transport double."""
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .alarms import State


@dataclass(frozen=True)
class Inbound:
    short_presses: int
    long_presses: int
    link_age_s: float
    unexpected_reset: bool
    button_stuck: bool


LINK_DOWN = Inbound(0, 0, math.inf, False, False)


class AlarmTransport(Protocol):
    def receive(self, monotonic_s: float) -> Inbound: ...
    def send(self, state: State, monotonic_s: float) -> None: ...


class SimulatedTransport:
    def __init__(self, *, heartbeat_ok: Callable[[float], bool]) -> None:
        self._heartbeat_ok = heartbeat_ok
        self._last_heartbeat_s = -math.inf
        self._short = 0
        self._long = 0
        self.reset_pending = False
        self.button_stuck = False
        self.last_sent: State | None = None
        self.fail = False

    def press(self) -> None:
        self._short += 1

    def hold(self) -> None:
        self._long += 1

    def receive(self, monotonic_s: float) -> Inbound:
        if self._heartbeat_ok(monotonic_s):
            self._last_heartbeat_s = monotonic_s
        inbound = Inbound(self._short, self._long, monotonic_s - self._last_heartbeat_s,
                          self.reset_pending, self.button_stuck)
        self._short = self._long = 0
        self.reset_pending = False
        return inbound

    def send(self, state: State, monotonic_s: float) -> None:
        if self.fail:
            raise RuntimeError("simulated transport failure")
        self.last_sent = state
