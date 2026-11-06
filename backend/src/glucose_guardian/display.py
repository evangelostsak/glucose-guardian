"""Display boundary; HDMI GUI implementation is deferred."""
from typing import Protocol

from .alarms import State
from .providers import Reading


class Display(Protocol):
    def render(self, reading: Reading | None, state: State) -> None: ...
