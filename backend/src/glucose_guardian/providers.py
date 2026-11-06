"""Acquisition boundary; simulated readings only."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Reading:
    mg_dl: float
    timestamp: datetime


class GlucoseProvider(Protocol):
    def read(self, now: datetime) -> Reading: ...


class SimulatedProvider:
    """Repeat a deterministic sequence, timestamped by the caller's clock."""

    def __init__(self, values: tuple[float, ...] = (110, 65, 50, 110)):
        if not values:
            raise ValueError("simulation requires at least one value")
        self.values = values
        self.index = 0

    def read(self, now: datetime) -> Reading:
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return Reading(value, now)
