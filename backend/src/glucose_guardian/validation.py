"""Reading validation, independent of alarm policy. Performs no I/O."""
from datetime import datetime
from enum import Enum
from math import isfinite

from .providers import Reading


class Validity(str, Enum):
    VALID = "valid"
    STALE = "stale"
    INVALID = "invalid"


def require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")


def validate(reading: Reading, *, now: datetime, max_age_s: float,
             max_future_skew_s: float) -> Validity:
    require_aware(now)
    value = reading.mg_dl
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Validity.INVALID
    if not isfinite(value) or value <= 0:
        return Validity.INVALID
    timestamp = reading.timestamp
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        return Validity.INVALID
    age_s = (now - timestamp).total_seconds()
    if age_s < -max_future_skew_s:
        return Validity.INVALID
    if age_s > max_age_s:
        return Validity.STALE
    return Validity.VALID
