"""Hardware boundary. No serial device is opened by this scaffold."""
from typing import Protocol
from .alarms import State


class AlarmTransport(Protocol):
    def send(self, state: State) -> None: ...
    def heartbeat_age(self, monotonic_s: float) -> float: ...
